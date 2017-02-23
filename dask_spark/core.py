import atexit
from collections import defaultdict
import logging
import subprocess
from time import sleep, time
from threading import Thread

from distributed import LocalCluster, Client
from distributed.comm.core import parse_host_port
from distributed.utils import sync
from tornado import gen
import pyspark

logger = logging.getLogger(__name__)


def start_master(dask_scheduler=None, port=7077):
    scheme, host, _ = dask_scheduler.address.split(':')
    host = host.strip(':/')
    proc = subprocess.Popen(['start-master.sh', '--host', host,
                                                '--port', str(port)])
    master = 'spark://%s:%d' % (host, port)
    logger.info("Start Spark at %s", master)
    dask_scheduler.extensions['spark'] = proc

    @atexit.register
    def remove_spark_master():
        proc.terminate()

    return master


def start_slave(master, cores, memory, dask_worker=None):
    proc = subprocess.Popen(['start-slave.sh', master,
                             '--cores', str(cores),
                             '--memory', str(int(memory)) + 'B'])
    dask_worker.extensions['spark'] = proc
    logger.info("Start Spark Slave, pointing to %s", master)

    @atexit.register
    def remove_spark_slave():
        proc.terminate()

    return 'OK'


@gen.coroutine
def _dask_to_spark(client):
    cluster_info = yield client.scheduler.identity()
    hosts = defaultdict(lambda: {'cores': 0, 'memory': 0})

    hosts = {}
    for addr, info in cluster_info['workers'].items():
        host = info['host']
        if host not in hosts:
            hosts[host] = {'address': addr, 'cores': 0, 'memory': 0}
        hosts[host]['cores'] += info['ncores']
        hosts[host]['memory'] += info.get('memory_limit', 4e9)

    if not hosts:
        raise ValueError("No workers found")

    master = yield client._run_on_scheduler(start_master)
    worker_futures = yield [client._run(start_slave, master,
                                        cores=h['cores'],
                                        memory=int(h['memory']),
                                        workers=[h['address']])
                            for h in hosts.values()]
    sc = pyspark.SparkContext(master)
    return sc


def dask_to_spark(client):
    """ Launch Spark Cluster on top of a Dask cluster

    Parameters
    ----------
    client: dask.distributed.Client

    Examples
    --------
    >>> from dask.distributed import Client  # doctest: +SKIP
    >>> client = Client('scheduler-address:8786')  # doctest: +SKIP
    >>> sc = dask_to_spark(client)  # doctest: +SKIP

    See Also
    --------
    spark_to_dask
    """
    return sync(client.loop, _dask_to_spark, client)


def start_worker(address):
    import distributed
    from distributed import Worker
    from tornado.ioloop import IOLoop

    if getattr(distributed, 'global_worker', False):
       return ['already occupied']

    loop = IOLoop.current()
    w = Worker(address, loop=loop)
    w.start(0)
    print("Started worker")

    distributed.global_worker = w

    from tornado import gen
    @gen.coroutine
    def _():
        while w.status != 'closed':
            yield gen.sleep(0.1)
    loop.run_sync(_)
    distributed.global_worker = False
    return ['completed']


def spark_to_dask(sc, loop=None):
    """ Launch a Dask cluster from a Spark Context

    Parameters
    ----------
    sc: pyspark.SparkContext

    Examples
    --------
    >>> sc = pyspark.SparkContext('spark://...')  # doctest: +SKIP
    >>> client = spark_to_dask(sc)  # doctest: +SKIP

    See Also
    --------
    dask_to_spark
    """
    cluster = LocalCluster(n_workers=0, loop=loop)
    rdd = sc.parallelize(range(1000))

    address = cluster.scheduler.address
    start_workers = lambda: rdd.mapPartitions(lambda x: start_worker(address)).count()
    thread = Thread(target=start_workers)
    thread.daemon = True
    thread.start()

    for i in range(1, 1000):
        if cluster.scheduler.workers:
            break
        if i % 100 == 0:
            print("Waiting for workers")
            logger.info("Waiting for workers")
        sleep(0.01)
        logger.info("%d", i)

    if i == 999:
        raise TimeoutError("No workers arrived")

    client = Client(cluster, loop=cluster.loop)
    return client

import logging

# Skip bson in requirements , pymongo provides
# noinspection PyPackageRequirements
from bson.timestamp import Timestamp
from copy_reg import pickle
from multiprocessing import Pool
from types import MethodType

from ResolverThread import ResolverThread
from mongodb_consistent_backup.Common import MongoUri, parse_method
from mongodb_consistent_backup.Errors import Error, OperationError
from mongodb_consistent_backup.Oplog import OplogState
from mongodb_consistent_backup.Pipeline import Task


# Allows pooled .apply_async()s to work on Class-methods:
def _reduce_method(m):
    if m.im_self is None:
        return getattr, (m.im_class, m.im_func.func_name)
    else:
        return getattr, (m.im_self, m.im_func.func_name)

pickle(MethodType, _reduce_method)


class Resolver(Task):
    def __init__(self, manager, config, timer, base_dir, backup_dir, tailed_oplogs, backup_oplogs):
        super(Resolver, self).__init__(self.__class__.__name__, manager, config, timer, base_dir, backup_dir)
        self.tailed_oplogs = tailed_oplogs
        self.backup_oplogs = backup_oplogs

        self.resolver_summary = {}
        self.resolver_state   = {}

        try:
            self._pool = Pool(processes=self.threads(None, 2))
        except Exception, e:
            logging.fatal("Could not start oplog resolver pool! Error: %s" % e)
            raise Error(e)

    def close(self):
       if self._pool:
           self._pool.terminate()
           self._pool.join()

    def get_consistent_end_ts(self):
        ts = None
        for shard in self.tailed_oplogs:
            instance = self.tailed_oplogs[shard]
            if 'last_ts' in instance and instance['last_ts'] is not None:
                if ts is None or instance['last_ts'].time < ts.time:
                    ts = Timestamp(instance['last_ts'].time, 0)
        return ts

    def run(self):
        logging.info("Resolving oplogs (options: threads=%s,compression=%s)" % (self.threads(), self.compression()))
        self.timer.start(self.timer_name)

        for shard in self.backup_oplogs:
            backup_oplog = self.backup_oplogs[shard]
            self.resolver_state[shard] = OplogState(self.manager, None, backup_oplog['file'])
            uri = MongoUri(backup_oplog['uri']).get()
            if shard in self.tailed_oplogs:
                tailed_oplog = self.tailed_oplogs[shard]
                tailed_oplog_file = tailed_oplog['file']
                if backup_oplog['last_ts'] is None and tailed_oplog['last_ts'] is None:
                    logging.info("No oplog changes to resolve for %s" % uri)
                elif backup_oplog['last_ts'] > tailed_oplog['last_ts']:
                    logging.fatal(
                        "Backup oplog is newer than the tailed oplog! This situation is unsupported. Please retry backup")
                    raise OperationError("Backup oplog is newer than the tailed oplog!")
                else:
                    try:
                        self._pool.apply_async(ResolverThread(
                            self.resolver_state[shard],
                            uri,
                            tailed_oplog.copy(),
                            backup_oplog.copy(),
                            self.get_consistent_end_ts(),
                            self.do_gzip()
                        ).run)
                    except Exception, e:
                        logging.fatal("Resolve failed for %s! Error: %s" % (uri, e))
                        raise Error(e)
            else:
                logging.info("No tailed oplog for host %s" % uri)
        self._pool.close()
        self._pool.join()

        self.timer.stop(self.timer_name)
        logging.info("Oplog resolving completed in %.2f seconds" % self.timer.duration(self.timer_name))

        for shard in self.resolver_state:
            self.resolver_summary[shard] = self.resolver_state[shard].get()
        return self.resolver_summary

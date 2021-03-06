# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2015, Numenta, Inc.  Unless you have purchased from
# Numenta, Inc. a separate commercial license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

import logging
import os
import traceback

from sqlalchemy import create_engine

from nta.utils import sqlalchemy_utils

import grok

from grok import grok_logging
from grok.app import config
from grok.app.repository.migrate import migrate
from grok.app.repository.queries import (
    addAnnotation,
    addAutostack,
    addDeviceNotificationSettings,
    addMetric,
    addMetricData,
    addMetricToAutostack,
    addNotification,
    batchAcknowledgeNotifications,
    batchSeeNotifications,
    clearOldNotifications,
    deleteAnnotationById,
    deleteAutostack,
    deleteMetric,
    deleteModel,
    deleteStaleNotificationDevices,
    getAllNotificationSettings,
    getAnnotationById,
    getAnnotations,
    getAutostack,
    getAutostackList,
    getAutostackFromMetric,
    getAutostackForNameAndRegion,
    getAutostackMetrics,
    getAutostackMetricsWithMetricName,
    getAutostackMetricsPendingDataCollection,
    getCloudwatchMetrics,
    getCloudwatchMetricsForNameAndServer,
    getCloudwatchMetricsPendingDataCollection,
    getCustomMetricByName,
    getCustomMetrics,
    getDeviceNotificationSettings,
    getInstanceCount,
    getInstances,
    getInstanceStatusHistory,
    getAllMetrics,
    getAllMetricsForServer,
    getAllModels,
    getMetric,
    getMetricWithSharedLock,
    getMetricWithUpdateLock,
    getMetricCountForServer,
    getMetricData,
    getMetricDataCount,
    getProcessedMetricDataCount,
    getMetricDataWithRawAnomalyScoresTail,
    getMetricIdsSortedByDisplayValue,
    getMetricStats,
    getNotification,
    getUnprocessedModelDataCount,
    getUnseenNotificationList,
    listMetricIDsForInstance,
    saveMetricInstanceStatus,
    setMetricCollectorError,
    setMetricLastTimestamp,
    setMetricStatus,
    updateDeviceNotificationSettings,
    updateMetricColumns,
    updateMetricColumnsForRefStatus,
    updateMetricDataColumns,
    updateNotificationDeviceTimestamp,
    updateNotificationMessageId,
    lockOperationExclusive,
    OperationLock)
from htmengine.repository import _EngineSingleton


retryOnTransientErrors = sqlalchemy_utils.retryOnTransientErrors


DSN_FORMAT = "mysql://%(user)s:%(passwd)s@%(host)s:%(port)s"
DB_DSN_FORMAT = "mysql://%(user)s:%(passwd)s@%(host)s:%(port)s/%(db)s"



g_log = logging.getLogger("grok.repository")



def getBaseConnectionArgsDict():
  """Return a dictonary of common database connection arguments."""
  return {
    "host": config.get("repository", "host"),
    "port": config.getint("repository", "port"),
    "user": config.get("repository", "user"),
    "passwd": config.get("repository", "passwd"),
    "charset": "utf8",
    "use_unicode": True,
  }



class _EngineSingleton(object):

  _dsn = None
  _engine = None
  _pid = None

  def __new__(cls, dsn, *args, **kwargs):
    """ Construct a new SQLAlchemy engine, returning a known engine if one
    exists, keeping track of the dsn used to create it.  If the dsn changes,
    dispose of the connection pool and reassign to a new engine instance.
    """
    pid = os.getpid()

    if cls._pid is not None:
      if cls._pid != pid:
        checkedin = cls._engine.pool.checkedin()
        checkedout = cls._engine.pool.checkedout()
        g_log.info(
          "_EngineSingleton.__new__(): forked process inherited engine=%s: "
          "oldPid=%s, newPid=%s, pool.checkedin=%s, pool.checkedout=%s",
          cls._engine, cls._pid, pid, checkedin, checkedout)

        if checkedin != 0:
          g_log.error("_EngineSingleton.__new__(): non-zero inherited "
                      "engine.pool.checkedin=%s", checkedin)
        if checkedout != 0:
          g_log.error("_EngineSingleton.__new__(): non-zero inherited "
                      "engine.pool.checkedout=%s", checkedout)

    if not cls._engine or not cls._dsn or (cls._dsn and cls._dsn != dsn):
      if cls._engine:
        # cls._engine may be set already, but the dsn has changed
        cls._engine.dispose()
        cls._engine = None

      cls._engine = create_engine(dsn, *args, **kwargs)
      cls._dsn = dsn
      cls._pid = os.getpid()
      if g_log.isEnabledFor(logging.DEBUG):
        # NOTE: checking isEnabledFor first because we don't want to pay the
        # price for traceback.format_stack unless we're actually going to log it
        g_log.debug(
          "_EngineSingleton.__new__(): created new engine=%s: "
          "pid=%s, pool.checkedin=%s, pool.checkedout=%s, callerStack=%s",
          cls._engine,
          cls._pid,
          cls._engine.pool.checkedin(),
          cls._engine.pool.checkedout(),
          traceback.format_stack(limit=10))


    return cls._engine # Note: Returning an instance of an engine, as returned
                       # by create_engine() rather than an instance of
                       # _EngineSingleton

  @classmethod
  def reset(cls):
    if cls._engine:
      cls._engine.dispose() # Explicitly dispose of the connection pool before
                            # deleting.
    cls._dsn = None
    cls._engine = None
    cls._pid = None



def getDSN():
  return DSN_FORMAT % dict(config.items("repository"))



def getUnaffiliatedEngine():
  return create_engine(getDSN())



def getDbDSN():
  config.loadConfig()
  return DB_DSN_FORMAT % dict(config.items("repository"))



def engineFactory(reset=False):
  """SQLAlchemy engine factory method

  See http://docs.sqlalchemy.org/en/rel_0_9/core/connections.html

  :param reset: Force a new engine instance.  By default, the same instance is
    reused when possible.
  :returns: SQLAlchemy engine object
  :rtype: sqlalchemy.engine.Engine

  Usage::

      from grok.app import repository
      engine = repository.engineFactory()
  """
  if reset:
    _EngineSingleton.reset()

  return _EngineSingleton(getDbDSN(), pool_recycle=179, pool_size=0,
                          max_overflow=-1)



def reset(offline=False):
  """
  Reset the grok database; upon successful completion, the necessary schema are
  created, but the tables are not populated

  :param offline: False to execute SQL commands; True to just dump SQL commands
    to stdout for offline mode or debugging
  """
  # Make sure we have the latest version of configuration
  config.loadConfig()
  dbName = config.get('repository', 'db')

  resetDatabaseSQL = (
      "DROP DATABASE IF EXISTS %(database)s; "
      "CREATE DATABASE %(database)s;" % {"database": dbName})
  statements = resetDatabaseSQL.split(";")

  engine = getUnaffiliatedEngine()
  with engine.connect() as connection:
    for s in statements:
      if s.strip():
        connection.execute(s)

  migrate(offline=offline)

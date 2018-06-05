##############################################################################
#
# Copyright (c) 2001 Zope Foundation and Contributors.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
import logging
import time

from six.moves._thread import allocate_lock
from six.moves._thread import get_ident

from DateTime import DateTime
from ZODB.POSException import ConflictError

import _mysql
from _mysql_exceptions import NotSupportedError
from _mysql_exceptions import OperationalError
from _mysql_exceptions import ProgrammingError
import MySQLdb
from MySQLdb.converters import conversions
from MySQLdb.constants import CLIENT
from MySQLdb.constants import CR
from MySQLdb.constants import ER
from MySQLdb.constants import FIELD_TYPE

from .joinTM import joinTM


LOG = logging.getLogger("ZMySQLDA")

hosed_connection = {
    CR.SERVER_GONE_ERROR: "Server gone.",
    CR.SERVER_LOST: "Server lost.",
    CR.COMMANDS_OUT_OF_SYNC: "Commands out of sync. Possibly a misplaced"
                             " semicolon (;) in a query."
}

query_syntax_error = (ER.BAD_FIELD_ERROR,)

key_types = {"PRI": "PRIMARY KEY", "MUL": "INDEX", "UNI": "UNIQUE"}

field_icons = "bin", "date", "datetime", "float", "int", "text", "time"

icon_xlate = {
    "varchar": "text",
    "char": "text",
    "enum": "what",
    "set": "what",
    "double": "float",
    "numeric": "float",
    "blob": "bin",
    "mediumblob": "bin",
    "longblob": "bin",
    "tinytext": "text",
    "mediumtext": "text",
    "longtext": "text",
    "timestamp": "datetime",
    "decimal": "float",
    "smallint": "int",
    "mediumint": "int",
    "bigint": "int",
}

type_xlate = {
    "double": "float",
    "numeric": "float",
    "decimal": "float",
    "smallint": "int",
    "mediumint": "int",
    "bigint": "int",
    "int": "int",
    "float": "float",
    "timestamp": "datetime",
    "datetime": "datetime",
    "time": "datetime",
}


def _mysql_timestamp_converter(s):
    if len(s) < 14:
        s = s + "0" * (14 - len(s))
        parts = map(int, (s[:4], s[4:6], s[6:8], s[8:10], s[10:12], s[12:14]))
    return DateTime("%04d-%02d-%02d %02d:%02d:%02d" % tuple(parts))


def DateTime_or_None(s):
    try:
        return DateTime(s)
    except Exception:
        return None


class DBPool(object):
    """
      This class is an interface to DB.
      Its caracteristic is that an instance of this class interfaces multiple
      instanes of DB class, each one being bound to a specific thread.
    """

    connected_timestamp = ""
    _create_db = False
    use_unicode = False

    def __init__(self, db_cls, create_db=False, use_unicode=False):
        """ Set transaction managed DB class for use in pool.
        """
        self.DB = db_cls
        # pool of one db object/thread
        self._db_pool = {}
        self._db_lock = allocate_lock()
        # auto-create db if not present on server
        self._create_db = create_db
        # unicode settings
        self.use_unicode = use_unicode

    def __call__(self, connection):
        """ Parse the connection string.

            Initiate a trial connection with the database to check
            transactionality once instead of once per DB instance.

            Create database if option is enabled and database doesn't exist.
        """
        self.connection = connection
        DB = self.DB
        db_flags = DB._parse_connection_string(connection, self.use_unicode)
        self._db_flags = db_flags

        # connect to server to determin tranasactional capabilities
        # can't use DB instance as it requires this information to work
        try:
            connection = MySQLdb.connect(**db_flags["kw_args"])
        except OperationalError:
            if self._create_db:
                kw_args = db_flags.get("kw_args", {}).copy()
                db = kw_args.pop("db", None)
                if not db:
                    raise
                connection = MySQLdb.connect(**kw_args)
                create_query = "create database %s" % db
                if self.use_unicode:
                    create_query += " default character set %s" % (
                                        DB.unicode_charset)
                connection.query(create_query)
                connection.store_result()
            else:
                raise
        transactional = connection.server_capabilities & CLIENT.TRANSACTIONS
        connection.close()

        # Some tweaks to transaction/locking db_flags based on server setup
        if db_flags["try_transactions"] == "-":
            transactional = False
        elif not transactional and db_flags["try_transactions"] == "+":
            raise NotSupportedError("transactions not supported by the server")
        db_flags["transactions"] = transactional
        del db_flags["try_transactions"]
        if transactional or db_flags["mysql_lock"]:
            db_flags["use_TM"] = True

        # will not be 100% accurate in regard to per thread connections
        # but as close as we're going to get it.
        self.connected_timestamp = DateTime()

        # return self as the database connection object
        # (assigned to _v_database_connection)
        return self

    def closeConnection(self):
        """ Close this threads connection. Used when DA is being reused
            but the connection string has changed. Need to close the DB
            instances and recreate to the new database. Only have to worry
            about this thread as when each thread hits the new connection
            string in the DA this method will be called.
        """
        ident = get_ident()
        try:
            self._pool_del(ident)
        except KeyError:
            pass

    def close(self):
        """ Used when manually closing the database. Resetting the pool
            dereferences the DB instances where they are then collected
            and closed.
        """
        self._db_pool = {}

    def _pool_set(self, key, value):
        """ Add a db to pool.
        """
        self._db_lock.acquire()
        try:
            self._db_pool[key] = value
        finally:
            self._db_lock.release()

    def _pool_get(self, key):
        """ Get db from pool. Read only, no lock necessary.
        """
        return self._db_pool.get(key)

    def _pool_del(self, key):
        """ Remove db from pool.
        """
        self._db_lock.acquire()
        try:
            del self._db_pool[key]
        finally:
            self._db_lock.release()

    def name(self):
        """ Return name of database connected to.
        """
        return self._db_flags["kw_args"]["db"]

    # Passthrough aliases for methods on DB class.
    def variables(self, *args, **kw):
        return self._access_db(method_id="variables", args=args, kw=kw)

    def tables(self, *args, **kw):
        return self._access_db(method_id="tables", args=args, kw=kw)

    def columns(self, *args, **kw):
        return self._access_db(method_id="columns", args=args, kw=kw)

    def query(self, *args, **kw):
        return self._access_db(method_id="query", args=args, kw=kw)

    def string_literal(self, *args, **kw):
        return self._access_db(method_id="string_literal", args=args, kw=kw)

    def unicode_literal(self, *args, **kw):
        return self._access_db(method_id="unicode_literal", args=args, kw=kw)

    def _access_db(self, method_id, args, kw):
        """
          Generic method to call pooled objects' methods.
          When the current thread had never issued any call, create a DB
          instance.
        """
        ident = get_ident()
        db = self._pool_get(ident)
        if db is None:
            db = self.DB(**self._db_flags)
            self._pool_set(ident, db)
        return getattr(db, method_id)(*args, **kw)


class DB(joinTM):

    Database_Error = _mysql.Error

    defs = {
        FIELD_TYPE.CHAR: "i",
        FIELD_TYPE.DATE: "d",
        FIELD_TYPE.DATETIME: "d",
        FIELD_TYPE.DECIMAL: "n",
        FIELD_TYPE.NEWDECIMAL: "n",
        FIELD_TYPE.DOUBLE: "n",
        FIELD_TYPE.FLOAT: "n",
        FIELD_TYPE.INT24: "i",
        FIELD_TYPE.LONG: "i",
        FIELD_TYPE.LONGLONG: "l",
        FIELD_TYPE.SHORT: "i",
        FIELD_TYPE.TIMESTAMP: "d",
        FIELD_TYPE.TINY: "i",
        FIELD_TYPE.YEAR: "i",
    }

    conv = conversions.copy()
    conv[FIELD_TYPE.LONG] = int
    conv[FIELD_TYPE.DATETIME] = DateTime_or_None
    conv[FIELD_TYPE.DATE] = DateTime_or_None
    conv[FIELD_TYPE.DECIMAL] = float
    conv[FIELD_TYPE.NEWDECIMAL] = float
    del conv[FIELD_TYPE.TIME]

    _p_oid = _p_changed = _registered = None

    unicode_charset = "utf8"  # hardcoded for now

    def __init__(self, connection=None, kw_args=None, use_TM=None,
                 mysql_lock=None, transactions=None):
        self.connection = connection  # backwards compat
        self._kw_args = kw_args
        self._mysql_lock = mysql_lock
        self._use_TM = use_TM
        self._transactions = transactions
        self._forceReconnection()

    def close(self):
        """ Close connection and dereference.
        """
        if getattr(self, "db", None):
            self.db.close()
            self.db = None

    __del__ = close

    def _forceReconnection(self):
        """ (Re)Connect to database.
        """
        try:  # try to clean up first
            self.db.close()
        except Exception:
            pass
        self.db = MySQLdb.connect(**self._kw_args)
        # Newer mysqldb requires ping argument to attmept a reconnect.
        # This setting is persistent, so only needed once per connection.
        self.db.ping(True)

    @classmethod
    def _parse_connection_string(cls, connection, use_unicode=False):
        """ Done as a class method to both allow access to class attribute
            conv (conversion) settings while allowing for wrapping pool class
            use of this method. The former is important to allow for subclasses
            to override the conv settings while the latter is important so
            the connection string doesn't have to be parsed for each instance
            in the pool.
        """
        kw_args = {"conv": cls.conv}
        flags = {"kw_args": kw_args, "connection": connection}
        if use_unicode:
            kw_args["use_unicode"] = use_unicode
            kw_args["charset"] = cls.unicode_charset
        items = connection.split()
        flags["use_TM"] = None
        if _mysql.get_client_info()[0] >= "5":
            kw_args["client_flag"] = CLIENT.MULTI_STATEMENTS
        if items:
            lockreq, items = items[0], items[1:]
            if lockreq[0] == "*":
                flags["mysql_lock"] = lockreq[1:]
                db_host, items = items[0], items[1:]
                flags["use_TM"] = True  # redundant. eliminate?
            else:
                flags["mysql_lock"] = None
                db_host = lockreq
            if "@" in db_host:
                db, host = db_host.split("@", 1)
                kw_args["db"] = db
                if ":" in host:
                    host, port = host.split(":", 1)
                    kw_args["port"] = int(port)
                kw_args["host"] = host
            else:
                kw_args["db"] = db_host
            if kw_args["db"] and kw_args["db"][0] in ("+", "-"):
                flags["try_transactions"] = kw_args["db"][0]
                kw_args["db"] = kw_args["db"][1:]
            else:
                flags["try_transactions"] = None
            if not kw_args["db"]:
                del kw_args["db"]
            if items:
                kw_args["user"], items = items[0], items[1:]
            if items:
                kw_args["passwd"], items = items[0], items[1:]
            if items:
                kw_args["unix_socket"], items = items[0], items[1:]

        return flags

    def tables(self, rdb=0, _care=("TABLE", "VIEW")):
        """ Returns list of tables.
        """
        r = []
        a = r.append
        result = self._query("SHOW TABLES")
        row = result.fetch_row(1)
        while row:
            table_name = row[0][0]
            a({"table_name": table_name, "table_type": "table"})
            row = result.fetch_row(1)
        return r

    def columns(self, table_name):
        """ Returns list of columns for [table_name].
        """
        try:
            # Field, Type, Null, Key, Default, Extra
            c = self._query("SHOW COLUMNS FROM %s" % table_name)
        except Exception:
            return ()
        r = []
        for Field, Type, Null, Key, Default, Extra in c.fetch_row(0):
            info = {}
            field_default = ""
            if Default is not None:
                info["default"] = Default
                field_default = "DEFAULT '%s'" % Default
            if "(" in Type:
                end = Type.rfind(")")
                short_type, size = Type[:end].split("(", 1)
                if short_type not in ("set", "enum"):
                    if "," in size:
                        info["scale"], info["precision"] = map(
                                int, size.split(",", 1))
                    else:
                        info["scale"] = int(size)
            else:
                short_type = Type
            if short_type in field_icons:
                info["icon"] = short_type
            else:
                info["icon"] = icon_xlate.get(short_type, "what")
            info["name"] = Field
            info["type"] = short_type
            info["extra"] = (Extra,)
            info["description"] = " ".join(
                [
                    Type,
                    field_default,
                    Extra or "",
                    key_types.get(Key, Key or ""),
                    Null == "NO" and "NOT NULL" or "",
                ]
            )
            info["nullable"] = (Null == "YES") and 1 or 0
            if Key:
                info["index"] = True
                info["key"] = Key
            if Key == "PRI":
                info["primary_key"] = True
                info["unique"] = True
            elif Key == "UNI":
                info["unique"] = True
            r.append(info)
        return r

    def variables(self):
        """ Return dictionary of current mysql variable/values.
        """
        # variable_name, value
        variables = self._query("SHOW VARIABLES")
        return dict((name, value) for name, value in variables.fetch_row(0))

    def _query(self, query, force_reconnect=False):
        """
          Send a query to MySQL server.
          It reconnects automaticaly if needed and the following conditions are
          met:
           - It has not just tried to reconnect (ie, this function will not
             attempt to connect twice per call).
           - This conection is not transactionnal and has set no MySQL locks,
             because they are bound to the connection. This check can be
             overridden by passing force_reconnect with True value.
        """
        try:
            self.db.query(query)
        except OperationalError as m:
            if m.args[0] in query_syntax_error:
                raise OperationalError(m.args[0],
                                       "%s: %s" % (m.args[1], query))

            if not force_reconnect and \
               (self._mysql_lock or self._transactions) or \
               m.args[0] not in hosed_connection:
                LOG.warning("query failed: %s" % (query,))
                raise
            # Hm. maybe the db is hosed.  Let's restart it.
            if m.args[0] in hosed_connection:
                msg = "%s Forcing a reconnect." % hosed_connection[m.args[0]]
                LOG.error(msg)
            self._forceReconnection()
            self.db.query(query)
        except ProgrammingError as m:
            if m.args[0] in hosed_connection:
                self._forceReconnection()
                msg = "%s Forcing a reconnect." % hosed_connection[m.args[0]]
                LOG.error(msg)
            else:
                LOG.warning("query failed: %s" % (query,))
            raise
        return self.db.store_result()

    def query(self, query_string, max_rows=1000):
        """ API method for making the query to mysql.
        """
        self._use_TM and self._register()
        desc = None
        result = ()
        for qs in filter(None, [q.strip() for q in query_string.split("\0")]):
            qtype = qs.split(None, 1)[0].upper()
            if qtype == "SELECT" and max_rows:
                qs = "%s LIMIT %d" % (qs, max_rows)
            c = self._query(qs)
            if desc is not None:
                if c and (c.describe() != desc):
                    msg = "Multiple select schema are not allowed."
                    raise ProgrammingError(msg)
            if c:
                desc = c.describe()
                result = c.fetch_row(max_rows)
            else:
                desc = None

            if qtype == "CALL":
                # For stored procedures, skip the status result
                self.db.next_result()

        if desc is None:
            return (), ()

        items = []
        func = items.append
        defs = self.defs
        for d in desc:
            item = {
                "name": d[0],
                "type": defs.get(d[1], "t"),
                "width": d[2],
                "null": d[6],
            }
            func(item)
        return items, result

    def string_literal(self, s):
        """ Called from zope to quote/escape strings for inclusion
            in a query.
        """
        return self.db.string_literal(s)

    def unicode_literal(self, s):
        """ Similar to string_literal but encodes it first.
        """
        return self.db.unicode_literal(s)

    # Zope 2-phase transaction handling methods
    def _begin(self, *ignored):
        """ Called from _register() upon first query.
        """
        try:
            self._transaction_begun = True
            self.db.ping()
            if self._transactions:
                self._query("BEGIN")
            if self._mysql_lock:
                self._query("SELECT GET_LOCK('%s',0)" % self._mysql_lock)
        except Exception:
            LOG.error("exception during _begin", exc_info=True)
            raise ConflictError

    def _finish(self, *ignored):
        """ Called as final event in transaction system.
        """
        if not self._transaction_begun:
            return
        self._transaction_begun = False
        try:
            if self._mysql_lock:
                self._query("SELECT RELEASE_LOCK('%s')" % self._mysql_lock)
            if self._transactions:
                self._query("COMMIT")
        except Exception:
            LOG.error("exception during _finish", exc_info=True)
            raise ConflictError

    def _abort(self, *ignored):
        """ Called in case of transactional error.
        """
        if not self._transaction_begun:
            return
        self._transaction_begun = False
        if self._mysql_lock:
            self._query("SELECT RELEASE_LOCK('%s')" % self._mysql_lock)
        if self._transactions:
            self._query("ROLLBACK")
        else:
            LOG.error("aborting when non-transactional")

    def _mysql_version(self):
        """ Return mysql server version.
            Note instances of this class are not persistent.
        """
        _version = getattr(self, "_version", None)
        if not _version:
            self._version = _version = self.variables().get("version")
        return _version

    def savepoint(self):
        """ Basic savepoint support.

            Raise AttributeErrors to trigger optimistic savepoint handling
            in zope's transaction code.
        """
        if self._mysql_version() < "5.0.2":
            # mysql supports savepoints in versions 5.0.3+
            LOG.warning("Savepoints unsupported with Mysql < 5.0.3")
            raise AttributeError

        if not self._transaction_begun:
            LOG.error("Savepoint used outside of transaction.")
            raise AttributeError

        return _SavePoint(self)


class _SavePoint(object):
    """ Simple savepoint object
    """

    def __init__(self, dm):
        self.dm = dm
        self.ident = ident = str(time.time()).replace(".", "sp")
        dm._query("SAVEPOINT %s" % ident)

    def rollback(self):
        self.dm._query("ROLLBACK TO %s" % self.ident)

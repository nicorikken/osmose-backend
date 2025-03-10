#-*- coding: utf-8 -*-

###########################################################################
##                                                                       ##
## Copyrights Etienne Chové <chove@crans.org> 2009                       ##
##                                                                       ##
## This program is free software: you can redistribute it and/or modify  ##
## it under the terms of the GNU General Public License as published by  ##
## the Free Software Foundation, either version 3 of the License, or     ##
## (at your option) any later version.                                   ##
##                                                                       ##
## This program is distributed in the hope that it will be useful,       ##
## but WITHOUT ANY WARRANTY; without even the implied warranty of        ##
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         ##
## GNU General Public License for more details.                          ##
##                                                                       ##
## You should have received a copy of the GNU General Public License     ##
## along with this program.  If not, see <http://www.gnu.org/licenses/>. ##
##                                                                       ##
###########################################################################

from modules import download
from modules.lockfile import lockfile
from modules.OsmOsis import OsmOsis
from modules.OsmState import OsmState
import sys
import os
import psycopg2
import fileinput
import shutil
import datetime
import time
try: # osmium still optional for now
    import osmium # type: ignore
except:
    pass
import dateutil


class OsmOsisManager:

  def __init__(self, conf, db_host, db_user, db_password, db_base, db_schema, db_persistent, logger):
    self.conf = conf

    self.db_host = db_host
    self.db_user = db_user
    self.db_base = db_base
    self.db_password = db_password
    self.db_schema = db_schema
    self.db_persistent = db_persistent
    self.logger = logger

    self.db_string = ""
    if self.db_host:
      self.db_string += "host=%s " % self.db_host
    self.db_string += "dbname=%s " % self.db_base
    self.db_string += "user=%s " % self.db_user
    self.db_string += "password=%s " % self.db_password
    self.db_string += "options=--search_path=%s,public " % (self.conf.db_schema_path or self.db_schema)

    self.db_psql_args = [self.db_string]

    if not self.check_database():
        raise Exception("Fail check database")

    # variable used by osmosis
    if not "JAVACMD_OPTIONS" in os.environ:
        os.environ["JAVACMD_OPTIONS"] = ""
    dir_country_tmp = os.path.join(self.conf.dir_tmp, self.db_schema)
    os.environ["JAVACMD_OPTIONS"] += " -Djava.io.tmpdir=" + dir_country_tmp
    os.environ["JAVACMD_OPTIONS"] += " -Duser.timezone=GMT"


  def __del__(self):
    if hasattr(self, '_osmosis') and self._osmosis:
      self._osmosis.close()


  def osmosis(self, schema_path=True):
    if not hasattr(self, '_osmosis'):
      if schema_path:
        self._osmosis = OsmOsis(self.db_string, self.conf.db_schema_path or self.db_schema)
      else:
        self._osmosis = OsmOsis(self.db_string)

    return self._osmosis


  def osmosis_close(self):
    if hasattr(self, '_osmosis'):
      self._osmosis.close()
      del self._osmosis


  def psql_c(self, sql):
    cmd  = ["psql"]
    cmd += self.db_psql_args
    cmd += ["-c", sql, "-v", "ON_ERROR_STOP=1"]
    self.logger.execute_out(cmd)


  def psql_f(self, script, cwd=None):
    cmd  = ["psql"]
    cmd += self.db_psql_args
    cmd += ["-f", script, "-v", "ON_ERROR_STOP=1"]
    self.logger.execute_out(cmd, cwd=cwd)


  def set_pgsql_schema(self, reset=False):
    if reset:
      db_schema = '"$user"'
    elif self.db_schema:
      db_schema = self.db_schema
    self.logger.log("set pgsql schema to %s" % db_schema)
    self.psql_c("ALTER ROLE %s IN DATABASE %s SET search_path = %s,public;" % (self.db_user, self.db_base, self.conf.db_schema_path or self.db_schema))

  def lock_database(self):
    osmosis_lock = False
    for trial in range(60):
      # acquire lock
      try:
        lfil = "/tmp/osmose-osmosis_import"
        osmosis_lock = lockfile(lfil)
        break
      except:
        self.logger.err("can't lock %s" % lfil)
        self.logger.log("waiting 2 minutes")
        time.sleep(2*60)

    if not osmosis_lock:
      self.logger.err("definitively can't lock")
      raise
    return osmosis_lock


  def check_database(self):
    # check if database contains all necessary extensions
    self.logger.sub().log("check database")
    gisconn = self.osmosis(schema_path = False).conn()
    giscurs = gisconn.cursor()
    for extension in ["hstore"] + self.conf.db_extension_check:
      giscurs.execute("SELECT installed_version FROM pg_available_extensions WHERE name = %s", [extension])
      if giscurs.rowcount != 1 or giscurs.fetchone()[0] is None:
        self.logger.err(u"missing extension: "+extension)
        return False

    giscurs.close()
    self.osmosis_close()

    return True


  def init_database(self, conf):
    # import osmosis

    # drop schema if present - might be remaining from a previous failing import
    self.logger.sub().log("DROP SCHEMA %s" % self.db_schema)
    gisconn = self.osmosis(schema_path=False).conn()
    giscurs = gisconn.cursor()
    giscurs.execute("DROP SCHEMA IF EXISTS %s CASCADE" % self.db_schema)
    giscurs.execute("CREATE SCHEMA %s" % self.db_schema)
    gisconn.commit()
    giscurs.close()
    self.osmosis_close()

    # schema
    self.logger.log(self.logger.log_av_r+"import osmosis schema"+self.logger.log_ap)
    for script in conf.osmosis_pre_scripts:
      self.psql_f(script)

    # data
    self.logger.log(self.logger.log_av_r+"import osmosis data"+self.logger.log_ap)
    cmd  = [conf.bin_osmosis]
    dst_ext = os.path.splitext(conf.download["dst"])[1]
    dir_country_tmp = os.path.join(self.conf.dir_tmp, self.db_schema)
    shutil.rmtree(dir_country_tmp, ignore_errors=True)
    os.makedirs(dir_country_tmp)
    if dst_ext == ".pbf":
      cmd += ["--read-pbf", "file=%s" % conf.download["dst"]]
    else:
      cmd += ["--read-xml", "file=%s" % conf.download["dst"]]
    cmd += ["-quiet"]
    cmd += ["--write-pgsql-dump", "directory=%s" % dir_country_tmp, "enableLinestringBuilder=yes"]

    try:
      self.logger.execute_err(cmd)

      for script in conf.osmosis_import_scripts:
        self.psql_f(script, cwd=dir_country_tmp)

    finally:
      # clean even in case of an exception
      shutil.rmtree(dir_country_tmp, ignore_errors=True)

    # post import scripts
    self.logger.log(self.logger.log_av_r+"import osmosis post scripts"+self.logger.log_ap)
    for script in conf.osmosis_post_scripts:
      self.psql_f(script)

    self.osmosis_close()


  def update_metainfo(self, conf):
    # Fill metainfo table
    gisconn = self.osmosis().conn()
    giscurs = gisconn.cursor()

    diff_path = conf.download["diff_path"]
    state_file = os.path.join(diff_path, "state.txt")
    dst_pbf = conf.download.get("dst")

    from modules.OsmPbf import OsmPbfReader
    osm_state = OsmPbfReader(dst_pbf, state_file=state_file).timestamp()

    giscurs.execute("UPDATE metainfo SET tstamp = %s", [osm_state])
    self.logger.sub().log("OSM data timestamp: {}".format(osm_state))

    gisconn.commit()
    giscurs.close()
    self.osmosis_close()


  def clean_database(self, conf, no_clean):
    gisconn = self.osmosis(schema_path = False).conn()
    giscurs = gisconn.cursor()

    if conf.db_persistent:
      pass

    elif no_clean:
      # grant read-only access to everybody
      self.logger.sub().log("GRANT USAGE %s" % self.db_schema)
      sql = "GRANT USAGE ON SCHEMA %s TO public" % self.db_schema
      self.logger.sub().log(sql)
      giscurs.execute(sql)
      for t in ["nodes", "ways", "way_nodes", "relations", "relation_members", "users", "schema_info", "metainfo"]:
         sql = "GRANT SELECT ON %s.%s TO public" % (self.db_schema, t)
         self.logger.sub().log(sql)
         giscurs.execute(sql)

    else:
      # drop all tables
      self.logger.sub().log("DROP SCHEMA %s" % self.db_schema)
      sql = "DROP SCHEMA IF EXISTS %s CASCADE;" % self.db_schema
      self.logger.sub().log(sql)
      giscurs.execute(sql)

    gisconn.commit()
    giscurs.close()
    self.osmosis_close()


  def check_osmosis_diff(self, conf):
    self.logger.log("check osmosis replication")
    diff_path = conf.download["diff_path"]
    if not os.path.exists(diff_path):
      return False

    for f_name in ["configuration.txt", "download.lock", "state.txt"]:
      f = os.path.join(diff_path, f_name)
      if not os.path.exists(f):
        return False

    return True


  def init_osmosis_diff(self, conf):
    self.logger.log(self.logger.log_av_r+"init osmosis replication for diff"+self.logger.log_ap)
    diff_path = conf.download["diff_path"]

    for f_name in ["configuration.txt", "download.lock", "state.txt"]:
        f = os.path.join(diff_path, f_name)
        if os.path.exists(f):
            os.remove(f)

    cmd  = [conf.bin_osmosis]
    cmd += ["--read-replication-interval-init", "workingDirectory=%s" % diff_path]
    cmd += ["-quiet"]
    self.logger.execute_err(cmd)

    for line in fileinput.input(os.path.join(diff_path, "configuration.txt"), inplace=1):
        if line.startswith("baseUrl"):
            sys.stdout.write("baseUrl=" + conf.download["diff"])
        elif line.startswith("maxInterval"):
            if "geofabrik" in conf.download["diff"]:
                # on daily diffs provided by Geofabrik, we should apply only one diff at a time
                sys.stdout.write("maxInterval=" + str(60*60*24//2)) # 1/2 day at most
            else:
                sys.stdout.write("maxInterval=" + str(7*60*60*24)) # 7 day at most
        else:
            sys.stdout.write(line)
    fileinput.close()

    download.dl(conf.download["state.txt"],
                os.path.join(diff_path, "state.txt"),
                self.logger.sub(),
                min_file_size=10)


  def run_osmosis_diff(self, conf):
    self.logger.log(self.logger.log_av_r+"run osmosis replication"+self.logger.log_ap)
    diff_path = conf.download["diff_path"]
    xml_change = os.path.join(diff_path, "change.osc.gz")
    tmp_pbf_file = conf.download["dst"] + ".tmp"

    shutil.copyfile(os.path.join(diff_path, "state.txt"),
                    os.path.join(diff_path, "state.txt.old"))

    dir_country_tmp = os.path.join(self.conf.dir_tmp, self.db_schema)
    shutil.rmtree(dir_country_tmp, ignore_errors=True)
    os.makedirs(dir_country_tmp)

    try:
      is_uptodate = False
      nb_iter = 0

      osm_state = OsmState(os.path.join(diff_path, "state.txt"))
      prev_state_ts = osm_state.timestamp()
      cur_ts = datetime.datetime.today()
      print("state: ", osm_state.timestamp(), end=' ')
      if osm_state.timestamp() < (cur_ts - datetime.timedelta(days=10)):
        # Skip updates, and directly download .pbf file if extract is too old
        self.logger.log(self.logger.log_av_r + "stop updates, to download full extract" + self.logger.log_ap)
        return (False, None)

      while not is_uptodate and nb_iter < 10:
        nb_iter += 1
        self.logger.log("iteration=%d" % nb_iter)

        try:
          cmd  = [conf.bin_osmosis]
          cmd += ["--read-replication-interval", "workingDirectory=%s" % diff_path]
          cmd += ["--simplify-change", "--write-xml-change", "file=%s" % xml_change]
          cmd += ["-quiet"]
          self.logger.execute_err(cmd)
        except:
          self.logger.log("waiting 2 minutes")
          time.sleep(2*60)
          continue

        cmd  = [conf.bin_osmosis]
        cmd += ["--read-xml-change", "file=%s" % xml_change]
        cmd += ["--read-pbf", "file=%s" % conf.download["dst"] ]
        cmd += ["--apply-change", "--buffer"]
        cmd += ["--write-pbf", "file=%s" % tmp_pbf_file]
        cmd += ["-quiet"]
        self.logger.execute_err(cmd)

        shutil.move(tmp_pbf_file, conf.download["dst"])

        # find if state.txt is more recent than one day
        osm_state = OsmState(os.path.join(diff_path, "state.txt"))
        cur_ts = datetime.datetime.today()
        print("state: ", nb_iter, " - ", osm_state.timestamp(), end=' ')
        if prev_state_ts is not None:
          print("   ", prev_state_ts - osm_state.timestamp())
        if osm_state.timestamp() > (cur_ts - datetime.timedelta(days=1)):
          is_uptodate = True
        elif prev_state_ts == osm_state.timestamp():
          is_uptodate = True
        else:
          prev_state_ts = osm_state.timestamp()

      if not is_uptodate:
        # we didn't get the latest version of the pbf file
        self.logger.log(self.logger.log_av_r + "didn't get latest version of osm file" + self.logger.log_ap)
        return (False, None)
      elif nb_iter == 1:
        return (True, xml_change)
      else:
        # TODO: we should return a merge of all xml change files
        return (True, None)

    except:
      shutil.rmtree(dir_country_tmp, ignore_errors=True)
      self.logger.err("got error, aborting")
      shutil.copyfile(os.path.join(diff_path, "state.txt.old"),
                      os.path.join(diff_path, "state.txt"))

      raise

  def check_osmium_diff(self, conf):
    self.logger.log("check osmium replication")
    pbf_file = conf.download["dst"]
    from osmium.replication.utils import get_replication_header # type: ignore

    try:
      # check if osmosis_* headers for replication are present
      url, seq, ts = get_replication_header(pbf_file)
      if url is None or seq is None or ts is None:
        return False
    except:
      # can happen if pbf file is corrupted
      return False

    return True

  def run_osmium_diff(self, conf):
    self.logger.log(self.logger.log_av_r + "run pyosmium replication" + self.logger.log_ap)
    pbf_file = conf.download["dst"]
    tmp_pbf_file = conf.download["dst"] + ".tmp.pbf"

    try:
      is_uptodate = False
      nb_iter = 0

      osmobject = osmium.io.Reader(pbf_file)
      prev_timestamp = dateutil.parser.isoparse(osmobject.header().get('osmosis_replication_timestamp'))
      cur_ts = datetime.datetime.now(datetime.timezone.utc)
      print("state: ", prev_timestamp, end=' ')
      if prev_timestamp < (cur_ts - datetime.timedelta(days=10)):
        # Skip updates, and directly download .pbf file if extract is too old
        self.logger.log(self.logger.log_av_r + "stop updates, to download full extract" + self.logger.log_ap)
        return (False, None)

      while not is_uptodate and nb_iter < 10:
        nb_iter += 1
        self.logger.log("iteration=%d" % nb_iter)

        if os.path.exists(tmp_pbf_file):
          os.remove(tmp_pbf_file)

        try:
          cmd  = [conf.bin_pyosmium_up_to_date]
          cmd += ["-v"]
          cmd += ["--outfile", tmp_pbf_file]
          cmd += ["--tmpdir", conf.dir_tmp]
          cmd += ["--force-update-of-old-planet"]
          cmd += [pbf_file]
          # pyosmium-up-to-date returns:
          #   - 0 on full update, or if pbf is already up-to-date
          #   - 1 on partial update
          ret = self.logger.execute_err(cmd, valid_return_code=(0, 1))

          if ret == 0 or ret == 1:
            shutil.move(tmp_pbf_file, pbf_file)

          if ret == 1:
            # we didn't fully update pbf file
            raise Exception("Not fully updated")

        except:
          self.logger.log("waiting 2 minutes")
          time.sleep(2*60)
          continue

        # Check if state.txt is more recent than one day
        osmobject = osmium.io.Reader(pbf_file)
        cur_timestamp = dateutil.parser.isoparse(osmobject.header().get('osmosis_replication_timestamp'))
        print("state: ", nb_iter, " - ", cur_timestamp, end=' ')
        print("   ", prev_timestamp - cur_timestamp)
        if cur_timestamp > (cur_ts - datetime.timedelta(days=1)):
          is_uptodate = True
        elif prev_timestamp == cur_timestamp:
          is_uptodate = True
        else:
          prev_timestamp = cur_timestamp

      if not is_uptodate:
        # we didn't get the latest version of the pbf file
        self.logger.log(self.logger.log_av_r + "didn't get latest version of osm file" + self.logger.log_ap)
        return (False, None)
      elif nb_iter == 1:
        return (True, None)
      else:
        # TODO: we should return a merge of all xml change files
        return (True, None)

    except:
      self.logger.err("got error, aborting")
      raise


  def check_change(self, conf):
    if not self.check_diff(conf):
      return False

    self.logger.log("check osmosis replication for database")

    return True


  def init_change(self, conf):
    self.init_osmosis_diff(conf)

    self.logger.log(self.logger.log_av_r+"import osmosis change post scripts"+self.logger.log_ap)
    self.set_pgsql_schema()
    for script in conf.osmosis_change_init_post_scripts:
        self.psql_f(script)
    self.set_pgsql_schema(reset=True)


  def run_change(self, conf):
    self.logger.log(self.logger.log_av_r+"run osmosis replication"+self.logger.log_ap)
    diff_path = conf.download["diff_path"]
    xml_change = os.path.join(diff_path, "change.osc.gz")

    shutil.copyfile(os.path.join(diff_path, "state.txt"),
                    os.path.join(diff_path, "state.txt.old"))

    try:
      osmosis_lock = self.lock_database()
      self.set_pgsql_schema()
      cmd  = [conf.bin_osmosis]
      cmd += ["--read-replication-interval", "workingDirectory=%s" % diff_path]
      cmd += ["--simplify-change", "--write-xml-change", "file=%s" % xml_change]
      cmd += ["-quiet"]
      self.logger.execute_err(cmd)

      self.psql_c("TRUNCATE TABLE actions")

      cmd  = [conf.bin_osmosis]
      cmd += ["--read-xml-change", xml_change]
      cmd += ["--write-pgsql-change", "database=%s" % conf.db_base, "user=%s" % conf.db_user, "password=%s" % conf.db_password]
      cmd += ["-quiet"]
      self.logger.execute_err(cmd)

      self.logger.log(self.logger.log_av_r+"import osmosis change post scripts"+self.logger.log_ap)
      for script in conf.osmosis_change_post_scripts:
        self.logger.log(script)
        self.psql_f(script)
      self.set_pgsql_schema(reset=True)
      del osmosis_lock

      # Fill metainfo table
      gisconn = self.osmosis().conn()
      giscurs = gisconn.cursor()

      osm_state = OsmState(os.path.join(diff_path, "state.txt"))
      osm_state_old = OsmState(os.path.join(diff_path, "state.txt.old"))
      giscurs.execute("UPDATE metainfo SET tstamp = %s, tstamp_action = %s", [osm_state.timestamp(), osm_state_old.timestamp()])

      gisconn.commit()
      giscurs.close()
      self.osmosis_close()

      return xml_change

    except:
      self.logger.err("got error, aborting")
      shutil.copyfile(os.path.join(diff_path, "state.txt.old"),
                      os.path.join(diff_path, "state.txt"))

      raise


  def run_resume(self, conf):
    self.logger.log(self.logger.log_av_r+"import osmosis resume post scripts"+self.logger.log_ap)
    self.set_pgsql_schema()
    for script in conf.osmosis_resume_init_post_scripts:
      self.psql_f(script)


  def postgis_version(self):
    # Need its own psql connecion, as it can be called when the main one is used
    gisconn = psycopg2.connect(self.db_string)
    giscurs = gisconn.cursor()
    sql = "SELECT PostGIS_version()"
    giscurs.execute(sql)
    version = list(map(int, giscurs.fetchone()[0].split(' ')[0].split('.')))
    giscurs.close()
    gisconn.close()

    return version

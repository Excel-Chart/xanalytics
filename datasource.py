#
# backend model for retrieving data, from files, BigQuery, and from ndb
#

import csv
import codecs
import models
import logging

import gsdata
import bqutil
import datetime
from collections import defaultdict, OrderedDict
from google.appengine.api import memcache

mem = memcache.Client()

NDB_DATASETS = {'staff': models.StaffUser}

class DataSource(object):
    '''
    Methods for retrieving data from files, BigQuery, and from ndb
    '''

    #-----------------------------------------------------------------------------

    def get_data(self, source, key=None):
        '''
        Get data from source, and return.
        source should either be string of the form "dataset.table" specifying a BigQuery source,
        or "docs:file_name:sheet_name" specifying a Google Spreadsheet source.
        '''
        if source.startswith('ndb:'):
            table = source[4:]
            return self.get_ndb_data(table)

        if source.startswith('docs:'):
            (fname, sheet) = source[5:].split(':',1)
            return gsdata.cached_get_datasheet(fname, sheet, key=key)

        if source.startswith('file:'):
            return self.get_datafile(source[5:], key=key)

        (dataset, table) = source.split('.')
        return self.cached_get_bq_table(dataset, table, key=key)

    @staticmethod
    def get_ndb_dataset(table):
        if table not in NDB_DATASETS:
            raise Exception("[datasource] Error!  Unknown NDB data set %s" % table)
        return NDB_DATASETS[table]

    def get_ndb_data(self, table, key=None):
        ndbset = self.get_ndb_dataset(table)
        ret = {'data': []}
        entity = None
        for entity in ndbset.query():
            de = { prop : getattr(entity, prop) for prop in entity._properties }
            ret['data'].append(de)
                
        if key is not None:
            ret['data_by_key'] = self.make_data_by_key(ret['data'], key)
                
        if entity is not None:
            ret['fields'] = entity._properties
            ret['field_names'] = ret['fields']
        return ret

    def import_data_to_ndb(self, data, table, overwrite=False):
        ndbset = self.get_ndb_dataset(table)

        if overwrite:
            models.ndb.delete_multi([x.key for x in ndbset.query()])

        for entry in data:
            entity = ndbset(**entry)
            entity.put()

    def get_datafile(self, fn, key=None):
        '''
        Get data from local csv file, and return.
        '''
        ret = {'data': []}
        with codecs.open('data/' + fn) as fp:
            for cdr in csv.DictReader(fp):
                ret['data'].append(cdr)

        if key is not None:
            ret['data_by_key'] = self.make_data_by_key(ret['data'], key)
                
        ret['fields'] = cdr.keys()
        ret['field_names'] = cdr.keys()
        return ret

    @staticmethod
    def make_data_by_key(data, key):
        if type(key)==dict:
            keyname = key['name']
        else:
            keyname = key
        data_by_key = OrderedDict()
        for row in data:
            the_key = row[keyname]
            if type(key)=='dict' and "keymap" in key:
                the_key = key['keymap'](the_key)
            data_by_key[the_key] = row
        return data_by_key


    def cached_get_bq_table(self, dataset, table, sql=None, key=None, drop=None,
                            logger=None, ignore_cache=False, startIndex=0, maxResults=1000000):
        '''
        Get a dataset from BigQuery; use memcache
        '''
        if logger is None:
            logger = logging.info
        memset = '%s.%s' % (dataset,table)
        if startIndex:
            memset += '-%d-%d' % (startIndex, maxResults)
        data = mem.get(memset)
        if (not data) or ignore_cache:
            try:
                data = bqutil.get_bq_table(dataset, table, sql, key=key, logger=logger,
                                           startIndex=startIndex, maxResults=maxResults)
            except Exception as err:
                logging.error(err)
                data = {'fields': {}, 'field_names': [], 'data': [], 'data_by_key': {}}
                return data		# don't cache empty result
            if (drop is not None) and drop:
                for key in drop:
                    data.pop(key)	# because data can be too huge for memcache ("Values may not be more than 1000000 bytes in length")
            try:
                mem.set(memset, data, time=3600*12)
            except Exception as err:
                logging.error('error doing mem.set for %s.%s from bigquery' % (dataset, table))
        self.bqdata[table] = data
        return data

    def fix_bq_dates(self, table):
        '''
        Using schema information, fix TIMESTAMP fields to display as dates.
        '''
        def map_field(idx, name):
            logging.info('Fixing timestamp for field %s' % name)
            for row in table['data']:
                # logging.info('row=%s' % row)
                if name in row and row[name]:
                    row[name] = str(datetime.datetime.utcfromtimestamp(float(row[name])))

        for k in range(0, len(table['fields'])):
            field = table['fields'][k]
            if field['type']=='TIMESTAMP':
                map_field(k, field['name'])
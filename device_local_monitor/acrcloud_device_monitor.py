#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os, sys, json
import logging, logging.handlers
import re
import threading
import hashlib
import urlparse
import time
import base64
import hmac
import subprocess
import multiprocessing
import platform
import ConfigParser
import requests
import urllib2
import Queue 
import datetime, mimetools

import acrcloud_stream_decode

reload(sys)
sys.setdefaultencoding("utf8")

class ACRCloudRecognizer:

    def __init__(self, config):
        self._config = config
        self._logger = config['logger']

    def create_fingerprint(self, buf):
        audio_fingerprint_opt = {
            'filter_energy_min': int(self._config['config']['rec']['filter_energy_min']),
            'silence_energy_threshold': int(self._config['config']['rec']['silence_energy_threshold']),
            'silence_rate_threshold': float(self._config['config']['rec']['silence_rate_threshold'])
        }
        return acrcloud_stream_decode.create_fingerprint(buf, False, audio_fingerprint_opt)

    def recognize(self, host, wav_buf, query_type, stream_id, access_key, access_secret, timeout=8):
        res = ''
        try:
            timeout = self._config['config']['rec']['timeout']
            fp_buf = self.create_fingerprint(wav_buf)
            if not fp_buf:
                return "{\"status\":{\"msg\":\"Unable To Generate Fingerprint, May Be Mute.\", \"code\":2004}}"
            self._logger.debug("fp length:" + str(len(fp_buf)))
            res = self._do_recogize(host, fp_buf, query_type, stream_id, access_key, access_secret, timeout)
        except Exception as e:
            self._logger.error(str(e))
        return res

    def _post_multipart(self, url, fields, files, timeout):
        content_type, body = self._encode_multipart_formdata(fields, files)
        if not content_type and not body:
            self._logger.error('encode_multipart_formdata error')
            return None
        try:
            req = urllib2.Request(url, data=body)
            req.add_header('Content-Type', content_type)
            req.add_header('Referer', url)
            resp = urllib2.urlopen(req, timeout=timeout)
            ares = resp.read()
            return ares
        except Exception, e:
            self._logger.error('post_multipart error', exc_info=True)
        return None

    def _encode_multipart_formdata(self, fields, files):
        try:
            boundary = mimetools.choose_boundary()
            CRLF = '\r\n'
            L = []
            for (key, value) in fields.items():
                L.append('--' + boundary)
                L.append('Content-Disposition: form-data; name="%s"' % key)
                L.append('')
                L.append(str(value))
            for (key, value) in files.items():
                L.append('--' + boundary)
                L.append('Content-Disposition: form-data; name="%s"; filename="%s"' % (key, key))
                L.append('Content-Type: application/octet-stream')
                L.append('')
                L.append(value)
            L.append('--' + boundary + '--')
            L.append('')
            body = CRLF.join(L)
            content_type = 'multipart/form-data; boundary=%s' % boundary
            return content_type, body
        except Exception, e:
            self._logger.error('encode_multipart_formdata error', exc_info=True)
        return None, None

    def _do_recogize(self, host, query_data, query_type, stream_id, access_key, access_secret, timeout=8):
        http_method = "POST"
        http_url_file = "/v1/monitor/identify" 
        data_type = query_type
        signature_version = "1"
        timestamp = int(time.mktime(datetime.datetime.utcfromtimestamp(time.time()).timetuple()))
        sample_bytes = str(len(query_data))

        string_to_sign = http_method+"\n"+http_url_file+"\n"+str(access_key)+"\n"+data_type+"\n"+signature_version+"\n"+str(timestamp)
        sign = base64.b64encode(hmac.new(str(access_secret), str(string_to_sign), digestmod=hashlib.sha1).digest())

        fields = {'access_key':access_key,
                  'stream_id':stream_id,
                  'sample_bytes':sample_bytes,
                  'timestamp':str(timestamp),
                  'signature':sign,
                  'data_type':data_type,
                  "signature_version":signature_version}

        server_url = 'http://' + str(host) + http_url_file
        res = self._post_multipart(server_url, fields, {"sample" : query_data}, timeout)
        return res

    
class MonitorProcess(multiprocessing.Process):

    def __init__(self, stream_info, config):
        multiprocessing.Process.__init__(self)
        self.daemon = True

        self._stream_info = stream_info
        self._config = config
        self._logger = config['logger']
        
    def start(self):
        try:
            self._logger.debug(self._stream_info['stream_url'] + ", process start")

            work_queue = Queue.Queue()
            decode_worker = self._DecodeStreamWorker(work_queue, self._stream_info, self._config)
            self._decode_worker = decode_worker
            decode_worker.start()
            process_worker = self._RecognizeWorker(work_queue, self._stream_info, self._config)
            self._process_worker = process_worker
            process_worker.start()

            self.wait()

            self._logger.debug(self._stream_info['stream_url'] + ", process end")
        except Exception as e:
            self._logger.error(str(e))

    def wait(self):
        try:
            self._decode_worker.join()
            self._process_worker.join()
        except Exception as e:
            self._logger.error(str(e))

    class _DecodeStreamWorker(threading.Thread):

        def __init__(self, worker_queue, stream_info, config):
            threading.Thread.__init__(self)
            self.setDaemon(True)
            self._config = config
            self._stream_url = stream_info['stream_url']
            self._stream_id = stream_info['stream_id']
            self._worker_queue = worker_queue
            self._interval = stream_info.get('interval', 10)
            self._monitor_length = stream_info.get('monitor_length', 10)
            self._download_timeout = self._config['config']['download']['read_timeout']
            self._open_timeout = self._config['config']['download']['open_timeout']
            self._detect_interval = self._config['config']['download'].get('detect_interval', 5)
            self._is_stop = True
            self._retry_n = 0
            self._logger = config['logger']

        def run(self):
            self._is_stop = False
            self._logger.info(' '.join([self._stream_id, self._stream_url, "DecodeStreamWorker running!"]))
            while not self._is_stop:
                try:
                    self._decode_stream(self._stream_url)
                    time.sleep(self._detect_interval)
                    self._retry_n = self._retry_n + 1
                except Exception as e:
                    self._logger.error(str(e))
            self._logger.info(' '.join([self._stream_id, self._stream_url, "DecodeStreamWorker stopped!"]))

        def _decode_stream(self, stream_url):
            try:
                acrdict = {
                    'callback_func': self._decode_callback,
                    'stream_url': stream_url,
                    'read_size_sec': self._interval + self._monitor_length,
                    'open_timeout_sec':self._open_timeout,
                    'read_timeout_sec':self._download_timeout,
                    'is_debug':1,
                    'extra_opt': {}
                }
                for ik,iv in self._config['config']['device'].items():
                    if iv != 0:
                        acrdict['extra_opt'][ik] = str(iv)
                        self._logger.info('device value -- ' + ik + ': ' + str(iv))
                code, msg, ffcode, ffmsg = acrcloud_stream_decode.decode_audio(acrdict)

                self._logger.error(' '.join([self._stream_id, self._stream_url, "URL:"+str(stream_url) + ", CODE:"+str(code) + ", MSG:"+str(msg)]))
                self._logger.error(' '.join([self._stream_id, self._stream_url, "URL:"+str(stream_url) + ", FFCODE:"+str(ffcode) + ", FFMSG:"+str(ffmsg)]))
            except Exception as e:
                self._logger.error(str(e))

        def _decode_callback(self, res_data):
            try:
                if self._is_stop:
                    return 1

                if res_data.get('audio_data') != None:
                    task = (1, res_data.get('audio_data'))
                    self._logger.debug(' '.join([self._stream_id, self._stream_url, ": audio len:" + str(len(res_data.get('audio_data')))]))
                    self._worker_queue.put(task, False)
                    self._logger.debug(' '.join([self._stream_id, self._stream_url, ": queue size:" + str(self._worker_queue.qsize())]))
                else:
                    self._logger.debug(' '.join([self._stream_id, self._stream_url, str(res_data)]))
                return 0
            except Exception as e:
                self._logger.error(' '.join([str(self._stream_id), self._stream_url, str(e)]))
        
    class _RecognizeWorker(threading.Thread):

        def __init__(self, worker_queue, stream_info, config):
            threading.Thread.__init__(self)
            self.setDaemon(True)
            self._config = config
            self._worker_queue = worker_queue
            self._stream_url = stream_info['stream_url']
            self._stream_id = stream_info['stream_id']
            self._stream_info = stream_info
            self._interval = stream_info.get('interval', 10)
            self._monitor_length = stream_info.get('monitor_length', 10)
            self._is_stop = True
            self._logger = config['logger']

            self._recognizer_ = ACRCloudRecognizer(config)

        def run(self):
            self._logger.info(' '.join([self._stream_id, self._stream_url, "RecognizeWorker running!"]))
            self._is_stop = False
            while not self._is_stop:
                try:
                    task = self._worker_queue.get()
                    self._worker_queue.task_done()
                    task_type, now_buf = task
                    host = self._stream_info['rec_host']
                    stream_id = self._stream_id
                    access_key = self._stream_info['access_key']
                    access_secret = self._stream_info['access_secret']
                    query_type = 'fingerprint'

                    wav_buf = now_buf[:self._monitor_length*8000*2]
                    rec_res = self._recognizer_.recognize(host, wav_buf, query_type, stream_id, access_key, access_secret)
                    self._logger.info(' '.join([self._stream_id, self._stream_url, str(rec_res)]))
                except Exception as e:
                    self._logger.error(str(e))
            self._logger.info(' '.join([self._stream_id, self._stream_url, "RecognizeWorker stopped!"]))

class DeviceMonitorClient():

    def __init__(self, config):
        self._config = config
        self._logger = config['logger']
        self._streams = []

    def run(self):
        try:
            s_remote_stream_info = self._get_remote_info()
            if not s_remote_stream_info:
                self._logger.error("can not get remote info.")
                return
                #sys.exit(1)

            j_remote_stream_info = json.loads(s_remote_stream_info)

            streams = []
            access_secret = j_remote_stream_info['access_secret']
            for st in j_remote_stream_info['streams']:
                st['access_secret'] = access_secret
                st['access_key'] = self._config['config']['api']['access_key']

                m_handler = MonitorProcess(st, self._config)
                m_handler.run()

                self._streams.append({'info':st, 'handler': m_handler})

            if not self._streams:
                self._logger.error("nothing can run.")
                return
                #sys.exit(1)
            
            self._check_alive()
        except Exception, e:
            self._logger.error(str(e))

    def _check_alive(self):
        while True:
            try:
                for mp in self._streams:
                    if not mp['handler'].is_alive():
                        #mp['handler'].wait()

                        m_handler = MonitorProcess(mp['info'], self._config)
                        m_handler.start()
                        mp['handler'] = m_handler
                self._logger.debug("checking finish.")
                time.sleep(10)
            except Exception, e:
                self._logger.error(str(e))

    def _get_remote_info(self):
        try:
            self._logger.info("read remote stream info.")

            surl = self._config['config']['api']['url']
            access_key = self._config['config']['api']['access_key']
            stream_ids = self._config['config']['api']['stream_ids']
            s_info_h = requests.get(surl, params={'access_key':access_key, 'stream_ids':stream_ids})
            s_info = s_info_h.text
            self._logger.debug(s_info)

            return s_info
        except Exception, e:
            self._logger.error(str(e))

def init_log(config):
    try:
        logger = logging.getLogger("monitor")
        logger_level_map = {'debug':logging.DEBUG, 'info':logging.INFO, 'warning':logging.WARNING, 'error':logging.ERROR}
        logger.setLevel(logger_level_map[config['log']['level']])
        if config['log']['file']:
            log_file_handler = logging.handlers.RotatingFileHandler(config['log']['file'], maxBytes=config['log']['max_size'], backupCount=1)
            log_file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
            logger.addHandler(log_file_handler)

        if config['log']['console']:
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
            logger.addHandler(ch)
        return logger 
    except Exception, e:
        print "init_log", e
        sys.exit(-1)

def parse_config():
    try:
        if not os.path.exists("acrcloud_config.ini"):
            print "acrcloud_config.ini is not exitsts."
            sys.exit()

        cf = ConfigParser.ConfigParser()
        cf.read('acrcloud_config.ini')

        config = {'api':{}, 'log':{}, 'rec':{}, 'download':{}, 'device':{}}

        config['api']['access_key'] = cf.get('api', 'access_key')
        if not config['api']['access_key']:
            print "access_key missing."
            sys.exit(1)
        config['api']['url'] = cf.get('api', 'url')
        config['api']['stream_ids'] = cf.get('api', 'stream_ids')

        config['log']['level'] = cf.get('log', 'level')
        if not config['log']['level']:
            config['log']['level'] = 'debug'
        config['log']['console'] = cf.getboolean('log', 'console')
        config['log']['file'] = cf.get('log', 'file')
        config['log']['max_size'] = cf.get('log', 'max_size')
        if config['log']['max_size']:
            config['log']['max_size'] = eval(config['log']['max_size'])
            if not config['log']['max_size']:
                config['log']['max_size'] = 10 * 1024 * 1024

        config['rec']['timeout'] = cf.getint('rec', 'timeout')
        config['rec']['filter_energy_min'] = cf.getint('rec', 'filter_energy_min')
        config['rec']['silence_energy_threshold'] = cf.getint('rec', 'silence_energy_threshold')
        config['rec']['silence_rate_threshold'] = cf.getfloat('rec', 'silence_rate_threshold')

        config['download']['open_timeout'] = cf.getint('download', 'open_timeout')
        config['download']['read_timeout'] = cf.getint('download', 'read_timeout')
        config['download']['detect_interval'] = cf.getint('download', 'detect_interval')

        if cf.has_section('device'):
            config['device']['sample_rate'] = cf.getint('device', 'sample_rate')
            config['device']['channels'] = cf.getint('device', 'channels')

        return config
    except Exception, e:
        print 'parse_config', e

def main():
    print "parse config"
    config = parse_config()
    
    print "init log"
    logger = init_log(config)

    g_config = {'logger':logger, 'config':config}

    client = DeviceMonitorClient(g_config)

    while True:
        client.run()
        logger.error("retry run!!!")
        time.sleep(config['download']['detect_interval'])

if __name__ == '__main__':
    main()
    

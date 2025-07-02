#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os, sys, json, queue
import logging, logging.handlers
import threading
import hashlib
import time
import base64
import hmac
import multiprocessing
import configparser
import requests

import acrcloud_stream_tool


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
        # return acrcloud_stream_tool.create_fingerprint(buf, False, 0, 0)
        return acrcloud_stream_tool.create_fingerprint_v2(buf, False, audio_fingerprint_opt['filter_energy_min'],
                                                          audio_fingerprint_opt['silence_energy_threshold'],
                                                          audio_fingerprint_opt['silence_rate_threshold'])

    def recognize(self, host, wav_buf, stream_id, access_key, access_secret, timeout=8):
        res = ''
        try:
            timeout = self._config['config']['rec']['timeout']
            fp_buf = self.create_fingerprint(wav_buf)
            if not fp_buf:
                return "{\"status\":{\"msg\":\"Unable To Generate Fingerprint, May Be Mute.\", \"code\":2004}}"
            self._logger.debug("fp length:" + str(len(fp_buf)))
            res = self._do_recogize(host, fp_buf, stream_id, access_key, access_secret, timeout)
        except Exception as e:
            self._logger.error(str(e))
        return res

    def _do_recogize(self, host, fp_buffer, stream_id, access_key, access_secret, timeout=8):
        http_method = "POST"
        http_url_file = '/v1/monitor/identify'
        server_url = 'https://' + host + http_url_file
        data_type = 'fingerprint'
        signature_version = "1"
        timestamp = int(time.time())
        string_to_sign = (http_method + "\n" + http_url_file + "\n" + access_key +
                          "\n" + data_type + "\n" + signature_version + "\n" + str(timestamp))
        hmac_res = hmac.new(access_secret.encode('ascii'), string_to_sign.encode('ascii'),
                            digestmod=hashlib.sha1).digest()
        sign = base64.b64encode(hmac_res).decode('ascii')

        files = {
            'sample': ('sample', fp_buffer, 'application/octet-stream'),
            'access_key': (None, access_key),
            'stream_id': (None, stream_id),
            'sample_bytes': (None, len(fp_buffer)),
            'timestamp': (None, str(timestamp)),
            'signature': (None, sign),
            'data_type': (None, data_type),
            'signature_version': (None, signature_version),
        }
        response = requests.post(server_url, files=files, timeout=timeout)
        response.raise_for_status()
        return response.text


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

            work_queue = queue.Queue()
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
            self.daemon = True
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
                    'open_timeout_sec': self._open_timeout,
                    'read_timeout_sec': self._download_timeout,
                    'is_debug': 1,
                    'extra_opt': {}
                }
                for ik, iv in self._config['config']['device'].items():
                    if iv != 0:
                        acrdict['extra_opt'][ik] = str(iv)
                        self._logger.info('device value -- ' + ik + ': ' + str(iv))
                print(acrdict)
                code, msg, ffcode, ffmsg = acrcloud_stream_tool.decode_audio(acrdict)

                self._logger.error(' '.join([self._stream_id, self._stream_url,
                                             "URL:" + str(stream_url) + ", CODE:" + str(code) + ", MSG:" + str(msg)]))
                self._logger.error(' '.join([self._stream_id, self._stream_url,
                                             "URL:" + str(stream_url) + ", FFCODE:" + str(ffcode) + ", FFMSG:" + str(
                                                 ffmsg)]))
            except Exception as e:
                self._logger.error(str(e))

        def _decode_callback(self, res_data):
            try:
                if self._is_stop:
                    return 1

                if res_data.get('audio_data') is not None:
                    task = (1, res_data.get('audio_data'))
                    self._logger.debug(' '.join(
                        [self._stream_id, self._stream_url, ": audio len:" + str(len(res_data.get('audio_data')))]))
                    self._worker_queue.put(task, False)
                    self._logger.debug(' '.join(
                        [self._stream_id, self._stream_url, ": queue size:" + str(self._worker_queue.qsize())]))
                else:
                    self._logger.debug(' '.join([self._stream_id, self._stream_url, str(res_data)]))
                return 0
            except Exception as e:
                self._logger.error(' '.join([str(self._stream_id), self._stream_url, str(e)]))

    class _RecognizeWorker(threading.Thread):

        def __init__(self, worker_queue, stream_info, config):
            threading.Thread.__init__(self)
            self.daemon = True
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

                    wav_buf = now_buf[:self._monitor_length * 8000 * 2]
                    rec_res = self._recognizer_.recognize(host, wav_buf, stream_id, access_key,
                                                          access_secret)
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

            j_remote_stream_info = json.loads(s_remote_stream_info)

            access_secret = j_remote_stream_info['access_secret']
            for st in j_remote_stream_info['streams']:
                st['access_secret'] = access_secret
                st['access_key'] = self._config['config']['api']['access_key']

                m_handler = MonitorProcess(st, self._config)
                m_handler.run()

                self._streams.append({'info': st, 'handler': m_handler})

            if not self._streams:
                self._logger.error("nothing can run.")
                return
                # sys.exit(1)

            self._check_alive()
        except Exception as e:
            self._logger.error(str(e))

    def _check_alive(self):
        while True:
            try:
                for mp in self._streams:
                    if not mp['handler'].is_alive():
                        # mp['handler'].wait()

                        m_handler = MonitorProcess(mp['info'], self._config)
                        m_handler.start()
                        mp['handler'] = m_handler
                self._logger.debug("checking finish.")
                time.sleep(10)
            except Exception as e:
                self._logger.error(str(e))

    def _get_remote_info(self):
        try:
            self._logger.info("read remote stream info.")

            surl = self._config['config']['api']['url']
            access_key = self._config['config']['api']['access_key']
            stream_ids = self._config['config']['api']['stream_ids']
            s_info_h = requests.get(surl, params={'access_key': access_key, 'stream_ids': stream_ids})
            s_info = s_info_h.text
            self._logger.debug(s_info)

            return s_info
        except Exception as e:
            self._logger.error(str(e))


def init_log(config):
    try:
        logger = logging.getLogger("monitor")
        logger_level_map = {'debug': logging.DEBUG, 'info': logging.INFO, 'warning': logging.WARNING,
                            'error': logging.ERROR}
        logger.setLevel(logger_level_map[config['log']['level']])
        if config['log']['file']:
            log_file_handler = logging.handlers.RotatingFileHandler(config['log']['file'],
                                                                    maxBytes=config['log']['max_size'], backupCount=1)
            log_file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
            logger.addHandler(log_file_handler)

        if config['log']['console']:
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
            logger.addHandler(ch)
        return logger
    except Exception as e:
        print("init_log", e)
        sys.exit(-1)


def parse_config():
    try:
        if not os.path.exists("acrcloud_config.ini"):
            print("acrcloud_config.ini is not exitsts.")
            sys.exit()

        cf = configparser.ConfigParser()
        cf.read('acrcloud_config.ini')

        config = {'api': {}, 'log': {}, 'rec': {}, 'download': {}, 'device': {}}

        config['api']['access_key'] = cf.get('api', 'access_key')
        if not config['api']['access_key']:
            print("access_key missing.")
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
    except Exception as e:
        print('parse_config', e)


def main():
    print("parse config")
    config = parse_config()

    print("init log")
    logger = init_log(config)

    g_config = {'logger': logger, 'config': config}

    client = DeviceMonitorClient(g_config)

    while True:
        client.run()
        logger.error("retry run!!!")
        time.sleep(config['download']['detect_interval'])


if __name__ == '__main__':
    main()


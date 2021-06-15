#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os, sys, time, re, json, math
import Queue, logging, logging.handlers
import threading, multiprocessing
import acrcloud_stream_tool
from acrcloud_stream_tool import Encoder as ACREncoder

reload(sys)
sys.setdefaultencoding("utf8")

class DeviceRecorder(multiprocessing.Process):
    def __init__(self, stream_info, config):
        multiprocessing.Process.__init__(self)
        self.daemon = True
        self.stream_info_ = stream_info
        self.config_ = config
        self.logger_ = logging.getLogger('device_record')
        
    def run(self):
        try:
            self.logger_.info("__DeviceRecorder %s starting." % (self.stream_info_['url']))
            work_queue = Queue.Queue()
            decode_worker = self._DecodeStreamWorker(work_queue, self.stream_info_, self.config_)
            self.decode_worker_ = decode_worker
            decode_worker.start()
            process_worker = self._EncodeStreamWorker(work_queue, self.stream_info_, self.config_)
            self.process_worker_ = process_worker
            process_worker.start()

            self.wait()
        except Exception as e:
            self.logger_.error(str(e))

    def wait(self):
        try:
            self.decode_worker_.join()
            self.process_worker_.join()
        except Exception as e:
            self.logger_.error(str(e))

    def stop(self):
        try:
            self.decode_worker_.stop()
            self.process_worker_.stop()
        except Exception as e:
            self.logger_.error(str(e))

    class _DecodeStreamWorker(threading.Thread):
    
        def __init__(self, work_queue, stream_info, config):
            threading.Thread.__init__(self)
            self.setDaemon(True)
            self.stream_url_ = stream_info['url']
            self.work_queue_ = work_queue
            self.config_ = config
            self.dec_interval_ = config.get('dec_interval', 10)
            self.check_interval_ = config.get('check_interval', 10)
            self.download_timeout_ = config.get('download_timeout', 20)
            self.out_sample_rate_ = config.get('out_sample_rate', 44100)
            self.out_channels_ = config.get('out_channels', 1)
            self.logger_ = logging.getLogger('device_record')
            self.debug_ = config.get('debug', 0)
            self.is_stop_ = True
    
        def stop(self):
            self._is_stop = True

        def run(self):
            self.logger_.debug("_DecodeStreamWorker starting: " + self.stream_url_)
            self._is_stop = False
            while not self._is_stop:
                try:
                    self._decode_stream(self.stream_url_)
                    self.logger_.debug("_DecodeStreamWorker waiting: " + str(self.check_interval_))
                    time.sleep(self.check_interval_)
                except Exception as e:
                    self.logger_.error(str(e))
            self.logger_.debug("_DecodeStreamWorker stop: " + self.stream_url_)
    
        def _decode_stream(self, stream_url):
            try:
                acrdict = {
                    'callback_func': self._decode_callback,
                    'stream_url': stream_url,
                    'read_size_sec':self.dec_interval_,
                    'open_timeout_sec':self.download_timeout_,
                    'read_timeout_sec':self.download_timeout_,
                    'out_channels': self.out_channels_,
                    'out_sample_rate': self.out_sample_rate_,
                    'is_debug': self.debug_,
                }
                code, msg, ff_code, ff_msg = acrcloud_stream_tool.decode_audio(acrdict)
                self.logger_.error("URL:"+str(stream_url) + ", CODE:"+str(code) + ", MSG:"+str(msg))
                self.logger_.error("URL:"+str(stream_url) + ", FFCODE:"+str(ff_code) + ", FFMSG:"+str(ff_msg))
            except Exception as e:
                self.logger_.error(str(e))
    
        def _decode_callback(self, res_data):
            try:
                if self._is_stop:
                    return 1

                if res_data.get('audio_data') != None:
                    audio_buf = res_data.get('audio_data')
                    task = (1, audio_buf, self.stream_url_)
                    self.logger_.debug("audio len:" + str(len(task[1])))
                    self.work_queue_.put(task)
                return 0
            except Exception as e:
                self.logger_.error(str(e))

    class _EncodeStreamWorker(threading.Thread):
    
        def __init__(self, work_queue, stream_info, config):
            threading.Thread.__init__(self)
            self.setDaemon(True)
            self.logger_ = logging.getLogger('device_record')
            self.work_queue_ = work_queue
            self.stream_url_ = stream_info['url']
            self.is_local_ = config.get('is_local', 0)
            self.work_queue_ = work_queue
            self.file_duration_s_ = config.get('file_duration_s', 60)
            self.out_sample_rate_ = config.get('out_sample_rate', 44100)
            self.out_channels_ = config.get('out_channels', 1)
            self.stream_flag_ = re.sub('\W', '', self.stream_url_.split('?')[0])
            self.file_prefix_ = self.stream_flag_
            self.file_prefix_ = config.get('file_prefix', self.file_prefix_)
            self.file_path_ = config.get('file_path', 'ACR')
            self._is_stop = True

        def _upload(self, oss_file_path, file_buffer):
            try:
                pass
            except Exception as e:
                self.logger_.error(str(e))

        def _upload_local(self, file_path, file_buffer):
            try:
                file_path = '/tmp/' + file_path
                file_dir = os.path.dirname(file_path)
                if not os.path.exists(file_dir):
                    os.makedirs(file_dir)
                out = open(file_path, 'wb')
                out.write(file_buffer)
                out.close()
                self.logger_.info(file_path + ' local save.')
            except Exception as e:
                self.logger_.error(str(e))
    
        def run(self):
            self.logger_.debug("_EncodeStreamWorker starting: " + self.stream_url_)
            self._is_stop = False
            enc_opt = {'sample_rate': self.out_sample_rate_, 'channels': self.out_channels_, 'type': 'mp3'}
            enc_session = ACREncoder(enc_opt)
            signal_qs = []
            while not self._is_stop:
                try:
                    task = self.work_queue_.get()
                    task_type, now_buf, ts = task
                    if task_type != 1:
                        continue
                    zcr = acrcloud_stream_tool.get_zcr(now_buf, 20)
                    self.logger_.info("%s get buffer %d %f." % (self.stream_url_, len(now_buf), zcr))
                    if (zcr < 0.005 or zcr > 0.25) and enc_session.get_duration_ms() <= 0:
                        self.logger_.info("silence: " + self.stream_url_)
                        continue
                    signal_qs.append(abs(zcr-0.05))
                    enc_session.write(now_buf)
                    now_dur = enc_session.get_duration_ms()
                    if now_dur/1000 >= self.file_duration_s_:
                        sig_mq = sum(signal_qs) / len(signal_qs)
                        signal_qs = []
                        sig_t = int(10-math.log1p(sig_mq)*60)
                        if sig_t < 1:
                            sig_t = 1
                        enc_buf = enc_session.read_all()
                        if not enc_buf:
                            self.logger_.error("can not get encode data: " + self.stream_url_)
                            continue
                        file_path_day = time.strftime("%Y%m%d", time.localtime())
                        file_name = '_'.join([self.file_prefix_, str(sig_t), time.strftime("%Y%m%d%H%M%S", time.localtime()) + '.mp3'])
                        file_path = '/'.join([self.file_path_, self.stream_flag_, file_name])
                        print(file_path)
                        if not self.is_local_:
                            self._upload(file_path, enc_buf)
                        else:
                            self._upload_local(file_path, enc_buf)
                        enc_session = ACREncoder(enc_opt)
                        self.logger_.info("Save %s Suss." % (file_name))
                except Exception as e:
                    self.logger_.error(str(e))
            self.logger_.debug("_EncodeStreamWorker stop: " + self.stream_url_)

def init_log(logging_level, log_file):
    try:
        logger1 = logging.getLogger('device_record')
        logger1.setLevel(logging_level)
        if log_file.strip():
            acrcloud_stream = logging.handlers.RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=1)
            acrcloud_stream.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
            acrcloud_stream.setLevel(logging_level)
            logger1.addHandler(acrcloud_stream)
        else:
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
            ch.setLevel(logging_level)
            logger1.addHandler(ch)
        return logger1
    except Exception as e:
        print str(e)
        sys.exit(-1)

def parse_args():
    gconfig = {}
    try:
        import argparse
        parser = argparse.ArgumentParser()

        parser.add_argument("local", help="save file locally, [0|1]", type=int)
        parser.add_argument("--duration", help="duration[s] of mp3 file. default: 60s", type=int)
        parser.add_argument("--debug", help="debug, [0|1]", type=int)

        args = parser.parse_args()

        gconfig['is_local'] = args.local
        
        if args.duration:
            gconfig['file_duration_s'] = args.duration

        if args.debug:
            gconfig['debug'] = args.debug
    except Exception as e:
        print(e)

    return gconfig

def start_service():
    init_log(logging.INFO, "/tmp/drecord.log")
    stream_list = []
    worker_list = []
    is_local = 1
    gconfig = parse_args()
    for i in range(1, 5):
        stream_url = 'plughw:%d,0?cmd_f=alsa' % (i)
        stream_info = {'url': stream_url}
        stream_list.append(stream_info)
        w = DeviceRecorder(stream_info, gconfig)
        w.start()
        worker_list.append(w)

    for w in worker_list:
        w.join()

if __name__ == '__main__':

    start_service()

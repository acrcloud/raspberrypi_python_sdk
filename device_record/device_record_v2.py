#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os, sys, time, json, shutil, requests, urllib, stat, commands, signal, re
import Queue, logging, logging.handlers
import threading
import argparse, ConfigParser
import psutil
import oss2
from acrcloud_stream_tool import Encoder as ACREncoder
from acrcloud_stream_tool import decode_audio as acrcloud_decode_audio

# for pi3B+
__DEVIDE_MAP = {
    1: 'usb-1.1.2',
    2: 'usb-1.1.3',
    3: 'usb-1.3',
    4: 'usb-1.2'
}
__DEVIDE_INFO_FILE = '/proc/asound/cards'

__VERSION = '1.0.0'

__DEVIDE_NAME = 'acr000000'
__DEVIDE_NAME_FILE = '/boot/device_name.txt'

__INIT_FLAG_FILE = '/home/pi/init_flag.txt'


class DecodeStreamWorker(threading.Thread):

    def __init__(self, work_queue, stream_info, config):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.stream_info_ = stream_info
        self.stream_url_ = stream_info['url']
        self.work_queue_ = work_queue
        self.config_ = config
        self.dec_interval_s_ = config.get('dec_interval_s', 10)
        self.check_interval_s_ = config.get('check_interval_s', 10)
        self.download_timeout_s_ = config.get('download_timeout_s', 20)
        self.out_sample_rate_ = config.get('out_sample_rate', 16000)
        self.out_channels_ = config.get('out_channels', 1)
        self.logger_ = logging.getLogger('device_record')
        self.debug_ = 0
        if config['log_level'] == 'debug':
            self.debug_ = 1
        self.channel_name_ = ''
        self.is_stop_ = True

    def stop(self):
        self._is_stop = True

    def run(self):
        self.logger_.debug("_DecodeStreamWorker starting: " + self.stream_url_)
        self._is_stop = False
        while not self._is_stop:
            try:
                channel_infos = self._get_channel_info()
                if channel_infos:
                    self.logger_.info("_DecodeStreamWorker channel name: " + json.dumps(channel_infos))
                    self.channel_name_ = channel_infos['name']
                    self.stream_url_ = channel_infos['url']
                    self.stream_info_['url'] = channel_infos['url']
                    self._decode_stream(self.stream_url_)
                self.logger_.info("_DecodeStreamWorker waiting: " + str(self.check_interval_s_))
                time.sleep(self.check_interval_s_)
            except Exception as e:
                self.logger_.error(str(e))
        self.logger_.debug("_DecodeStreamWorker stop: " + self.stream_url_)

    def _decode_stream(self, stream_url):
        try:
            acrdict = {
                'callback_func': self._decode_callback,
                'stream_url': stream_url,
                'read_size_sec': self.dec_interval_s_,
                'open_timeout_sec': self.download_timeout_s_,
                'read_timeout_sec': self.download_timeout_s_,
                'out_channels': self.out_channels_,
                'out_sample_rate': self.out_sample_rate_,
                'is_debug': self.debug_,
            }
            code, msg, ff_code, ff_msg = acrcloud_decode_audio(acrdict)
            self.logger_.error("URL:" + str(stream_url) + ", CODE:" + str(code) + ", MSG:" + str(msg))
            self.logger_.error("URL:" + str(stream_url) + ", FFCODE:" + str(ff_code) + ", FFMSG:" + str(ff_msg))
        except Exception as e:
            self.logger_.error(str(e))

    def _decode_callback(self, res_data):
        try:
            if self._is_stop:
                return 1

            if res_data.get('audio_data'):
                audio_buf = res_data.get('audio_data')
                if not self.channel_name_:
                    self.channel_name_ = str(self.stream_info_['usb_id'])
                task = (1, audio_buf, self.stream_url_, self.channel_name_)
                self.logger_.debug("audio len:" + str(len(task[1])))
                self.work_queue_.put(task)
            return 0
        except Exception as e:
            self.logger_.error(str(e))

    def _get_channel_info(self):
        try:
            g_vars = globals()
            all_device_infos = []
            with open(g_vars['__DEVIDE_INFO_FILE']) as f:
                all_device_infos = f.readlines()
            usb_id = self.stream_info_['usb_id']
            usb_flag = g_vars['__DEVIDE_MAP'][usb_id]
            card_id = -1
            for i, info_line in enumerate(all_device_infos):
                if info_line.find(usb_flag) >= 0:
                    card_id = all_device_infos[i - 1].strip().split()[0]
            if card_id == -1:
                return None
            stream_url = 'plughw:%s,0?cmd_f=alsa' % card_id
            self.stream_url_ = stream_url
            return {
                'name': self.config_['names'][usb_id - 1],
                'card_id': card_id,
                'url': stream_url
            }
        except Exception as e:
            self.logger_.error(str(e))


class UploadWorker(threading.Thread):

    def __init__(self, work_queue, stream_info, config):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.logger_ = logging.getLogger('device_record')
        self.work_queue_ = work_queue
        self.stream_info_ = stream_info
        self.stream_url_ = stream_info['url']
        self.config_ = config
        self.is_stop_ = True

    def run(self):
        self.logger_.debug("_UploadWorker starting: " + self.stream_url_)
        self.is_stop_ = False
        oss_retry_interval_s = self.config_.get('oss_retry_interval_s', 10)
        while not self.is_stop_:
            try:
                local_file_path = self.work_queue_.get()
                while True:
                    if not local_file_path or not os.path.exists(local_file_path):
                        break
                    ret = self._upload_oss(local_file_path)
                    if ret:
                        self.logger_.info("oss saved %s suss." % local_file_path)
                        break
                    else:
                        self.logger_.error("oss saved %s fail." % local_file_path)
                        time.sleep(oss_retry_interval_s)
            except Exception as e:
                self.logger_.error(str(e))
        self.logger_.debug("_UploadWorker stop: " + self.stream_url_)

    def _upload_oss(self, local_file_path):
        ret = False
        try:
            if not self.config_.get('oss_access_key'):
                self.logger_.error("no oss_access_key")
                ret = True
                return ret
            file_name = local_file_path.strip().split('/')[-1]
            oss_access_key = self.config_.get('oss_access_key')
            oss_access_secret = self.config_.get('oss_access_secret', '')
            oss_host = self.config_.get('oss_host')
            oss_bucket_name = self.config_.get('oss_bucket_name')
            oss_connect_timeout_s = self.config_.get('oss_connect_timeout_s', 60)
            prefix_file_path = self.config_.get('file_path', 'ACR')
            file_path = '/'.join([prefix_file_path, file_name[:8], file_name])
            auth = oss2.Auth(oss_access_key, oss_access_secret)
            endpoint = 'https://' + oss_host
            bucket = oss2.Bucket(auth, endpoint, oss_bucket_name, connect_timeout=oss_connect_timeout_s)

            result = bucket.put_object_from_file(file_path, local_file_path)
            if result.status == 200:
                self.logger_.info(file_path + ' oss save suss.')
                ret = True
            else:
                self.logger_.info(file_path + ' oss save fail.')
        except Exception as e:
            self.logger_.error(str(e))
        return ret


class DeviceRecorder(threading.Thread):
    def __init__(self, stream_info, config):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.stream_info_ = stream_info
        self.config_ = config
        self.logger_ = logging.getLogger('device_record')
        self.decode_queue_ = Queue.Queue()
        self.upload_queue_ = Queue.Queue()
        self.decode_worker_ = None
        self.upload_worker_ = None

    def run(self):
        self.logger_.info("__DeviceRecorder %s starting." % (self.stream_info_['usb_id']))
        try:
            self.decode_worker_ = DecodeStreamWorker(self.decode_queue_, self.stream_info_, self.config_)
            self.decode_worker_.start()

            if self.config_.get('oss_enable'):
                self.upload_worker_ = UploadWorker(self.upload_queue_, self.stream_info_, self.config_)
                self.upload_worker_.start()

            self.process_audio()
        except Exception as e:
            self.logger_.error(str(e))
        self.logger_.info("__DeviceRecorder %s stop." % (self.stream_info_['usb_id']))

    def process_audio(self):
        out_sample_rate = self.config_.get('out_sample_rate', 16000)
        out_channels = self.config_.get('out_channels', 1)
        is_save_time_aligned = self.config_.get('is_save_time_aligned', 1)
        file_duration_s = self.config_.get('file_duration_s', 600)
        oss_enable = self.config_.get('oss_enable', 0)
        stream_url = self.stream_info_['url']

        self.logger_.debug("process_audio stop: " + stream_url)
        audio_buf_list = []
        while True:
            try:
                task = self.decode_queue_.get()
                task_type, now_buf, channel_url, channel_name = task
                if task_type != 1:
                    continue
                audio_buf_list.append(now_buf)
                now_duration_s = sum([len(tbuf) for tbuf in audio_buf_list]) / (
                        out_sample_rate * out_channels * 2)

                save_pcm_buffer = None
                save_time_str = ''
                now_time = time.localtime()
                if is_save_time_aligned:
                    time_s = now_time.tm_min * 60 + now_time.tm_sec
                    time_s_mod = time_s % file_duration_s
                    if time_s_mod < now_duration_s:
                        save_pcm_buffer_t = ''.join(audio_buf_list)
                        save_time_s = now_duration_s - time_s_mod + 1
                        save_file_bytes = save_time_s * out_sample_rate * out_channels * 2
                        audio_buf_list = [save_pcm_buffer_t[save_file_bytes:]]
                        save_time_t = time.mktime(now_time) - time_s_mod
                        save_pcm_buffer = save_pcm_buffer_t[:save_file_bytes]
                        if file_duration_s - save_time_s > 2:
                            self.logger_.info(stream_url + ' fill seconds: ' + str(file_duration_s - save_time_s))
                            fill_size = (file_duration_s - save_time_s) * out_sample_rate * out_channels * 2
                            save_pcm_buffer = '\0' * fill_size + save_pcm_buffer
                        save_time_str = time.strftime("%Y%m%d%H%M%S", time.localtime(save_time_t))
                elif now_duration_s >= file_duration_s:
                    save_pcm_buffer = ''.join(audio_buf_list)
                    save_time_str = time.strftime("%Y%m%d%H%M%S", now_time)

                if not save_pcm_buffer:
                    continue

                enc_buf = self._encode_audio(save_pcm_buffer)
                if not enc_buf:
                    self.logger_.error("can not get encode data: " + stream_url)
                    continue

                if channel_name[:3] == 'usb':
                    prefix = '-'.join(self.stream_info_['ip'].split('.')) + '_' + self.stream_info_['mac'] + '_' + \
                             self.stream_info_['device_name']
                    channel_name = prefix + '_' + channel_name
                file_name = '.'.join([save_time_str, channel_name, 'mp3'])

                local_file_path = self._save_local(enc_buf, file_name)
                if local_file_path and oss_enable:
                    self.upload_queue_.put(local_file_path)
                self._clean_expired_file()

                self.logger_.info("Saved %s." % file_name)
            except Exception as e:
                self.logger_.error(str(e))
        self.logger_.debug("process_audio stop: " + stream_url)

    def _encode_audio(self, pcm_buffer):
        out_sample_rate = self.config_.get('out_sample_rate', 16000)
        out_channels = self.config_.get('out_channels', 1)
        enc_buf = None
        try:
            enc_opt = {'sample_rate': out_sample_rate, 'channels': out_channels, 'type': 'mp3'}
            enc_session = ACREncoder(enc_opt)
            enc_session.write(pcm_buffer)
            enc_buf = enc_session.read_all()
        except Exception as e:
            self.logger_.error(str(e))
        return enc_buf

    def _save_local(self, file_buffer, file_name):
        file_path = self.config_.get('file_path', 'ACR')
        local_root_dir = self.config_.get('local_root_dir', '.')
        try:
            file_path = '/'.join([local_root_dir, file_path, file_name[:8], file_name])
            file_dir = os.path.dirname(file_path)
            if not os.path.exists(file_dir):
                os.makedirs(file_dir)
            out = open(file_path, 'wb')
            out.write(file_buffer)
            out.close()
            self.logger_.info(file_path + ' local save.')
            return file_path
        except Exception as e:
            self.logger_.error(str(e))

    def _clean_expired_file(self):
        file_path = self.config_.get('file_path', 'ACR')
        local_root_dir = self.config_.get('local_root_dir', '.')
        scan_file_path = '/'.join([local_root_dir, file_path])
        local_save_days = self.config_.get('local_save_days', 7)
        for f in os.listdir(scan_file_path):
            try:
                ts = time.mktime(time.strptime(f.strip(), "%Y%m%d"))
                self.logger_.debug(file_path + '/' + f.strip())
                if time.time() - ts > local_save_days * 24 * 3600:
                    del_path = file_path + '/' + f.strip()
                    self.logger_.warn('del: ' + del_path)
                    shutil.rmtree(del_path)
            except Exception as e:
                self.logger_.error(str(e))


def init_log(g_config):
    try:
        log_level_map = {
            'info': logging.INFO,
            'debug': logging.DEBUG,
            'warn': logging.WARNING,
            'error': logging.ERROR
        }
        logging_level = log_level_map[g_config['log_level']]
        log_file = g_config['log_file']
        logger1 = logging.getLogger('device_record')
        logger1.setLevel(logging_level)

        if log_file.strip():
            acrcloud_stream = logging.handlers.RotatingFileHandler(log_file, maxBytes=g_config['log_file_maxsize'],
                                                                   backupCount=1)
            acrcloud_stream.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
            acrcloud_stream.setLevel(logging_level)
            logger1.addHandler(acrcloud_stream)

        if g_config['log_console']:
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
            ch.setLevel(logging_level)
            logger1.addHandler(ch)

        return logger1
    except Exception as e:
        print(e)
        sys.exit(-1)


def get_version():
    return __VERSION


def parse_args():
    g_config = {
        'log_level': 'info',
        'log_console': 0,
        'log_file': '/tmp/drecord.log',
        'log_file_maxsize': 10 * 1024 * 1024,

        'dec_interval_s': 10,
        'check_interval_s': 10,
        'download_timeout_s': 20,
        'out_sample_rate': 16000,
        'out_channels': 1,

        'file_duration_s': 600,
        'is_save_time_aligned': 1,
        'file_path': 'ACR',

        'local_save_days': 7,
        'local_root_dir': '.',

        'check_update_interval_s': 600,

        'config_url': '',

        'oss_enable': 0,
        'oss_access_key': '',
        'oss_access_secret': '',
        'oss_host': '',
        'oss_bucket_name': '',
        'oss_connect_timeout_s': 60,
        'oss_retry_interval_s': 10,

        'names': ['usb' + str(i) for i in range(1, 5)]
    }

    try:
        parser = argparse.ArgumentParser()

        parser.add_argument("-c", '--config_file', help="config file path")
        parser.add_argument('-V', '--version', action='version', version=get_version(), help='Display version')

        args = parser.parse_args()

        if not args.config_file:
            return g_config

        config_filepath = args.config_file
        cp = ConfigParser.ConfigParser()
        cp.read(config_filepath)

        if cp.has_section('log'):
            if cp.has_option('log', 'level'):
                g_config['log_level'] = cp.get('log', 'level')
            if cp.has_option('log', 'console'):
                g_config['log_console'] = cp.get('log', 'console')
                if g_config['log_console'] == 'true':
                    g_config['log_console'] = 1
            if cp.has_option('log', 'file'):
                g_config['log_file'] = cp.get('log', 'file')
            if cp.has_option('log', 'max_size'):
                g_config['log_file_maxsize'] = cp.get('log', 'max_size')
                size_map = {'k': 1024, 'm': 1024 * 1024, 'g': 1024 * 1024 * 1024}
                unit = size_map.get(g_config['log_file_maxsize'].lower()[-1])
                if unit:
                    g_config['log_file_maxsize'] = int(g_config['log_file_maxsize'][:-1]) * unit
                else:
                    g_config['log_file_maxsize'] = int(g_config['log_file_maxsize'])
        if cp.has_section('system'):
            system_fields = ['dec_interval_s', 'check_interval_s', 'download_timeout_s', 'out_sample_rate',
                             'check_update_interval_s', 'out_channels', 'file_duration_s', 'is_save_time_aligned',
                             'local_save_days']
            for cfi in system_fields:
                if cp.has_option('system', cfi):
                    g_config[cfi] = cp.getint('system', cfi)
            if cp.has_option('system', 'config_url'):
                g_config['config_url'] = cp.get('system', 'config_url')
            if cp.has_option('system', 'file_path'):
                g_config['file_path'] = cp.get('system', 'file_path')
            if cp.has_option('system', 'local_root_dir'):
                g_config['local_root_dir'] = cp.get('system', 'local_root_dir')
            if cp.has_option('system', 'channel_names'):
                g_config['names'] = cp.get('system', 'channel_names')
                g_config['names'] = g_config['names'].split(',')
        if cp.has_section('oss'):
            oss_fields = ['access_key', 'access_secret', 'host', 'bucket_name']
            for cfi in oss_fields:
                if cp.has_option('oss', cfi):
                    g_config['oss_' + cfi] = cp.get('oss', cfi)
            if cp.has_option('oss', 'connect_timeout_s'):
                g_config['oss_connect_timeout_s'] = cp.getint('oss', 'connect_timeout_s')
            if cp.has_option('oss', 'enable'):
                g_config['oss_enable'] = cp.getint('oss', 'enable')
                g_config['oss_connect_timeout_s'] = cp.getint('oss', 'connect_timeout_s')
            if cp.has_option('oss', 'enable'):
                g_config['oss_enable'] = cp.getint('oss', 'enable')
        if cp.has_section('device'):
            global __DEVIDE_MAP
            if cp.has_option('device', 'map'):
                d_map = cp.get('device', 'map')
                for dmv in d_map.split(','):
                    dtl = dmv.split(':')
                    if len(dtl) != 2:
                        continue
                    __DEVIDE_MAP[dtl[0]] = dtl[1]
    except Exception as e:
        print(e)
        sys.exit(1)

    return g_config


def get_all_device_infos():
    device_infos = {}
    try:
        device_infos['cpu_count'] = psutil.cpu_count()
        device_infos['cpu_percent'] = psutil.cpu_percent()
        device_infos['cpu_temperatures'] = psutil.sensors_temperatures()['cpu_thermal'][0].current
        device_infos['loads'] = psutil.getloadavg()
        virtual_memory = psutil.virtual_memory()
        device_infos['memory'] = {'total': virtual_memory.total, 'available': virtual_memory.available}
        disk_info = psutil.disk_usage('/')
        device_infos['disk'] = {'total': disk_info.total, 'available': virtual_memory.free}

        net_addrs = {}
        for nk, kv in psutil.net_if_addrs().items():
            if nk == 'lo':
                continue
            t_nodes = {}
            for kv_item in kv:
                addr_type = ''
                if len(kv_item.address.split(':')) == 6:
                    addr_type = 'mac'
                if len(kv_item.address.split('.')) == 4:
                    addr_type = 'ip'
                if not addr_type:
                    continue
                t_nodes[addr_type] = kv_item.address
            net_addrs[nk] = t_nodes
        device_infos['net_addrs'] = net_addrs
    except Exception as e:
        print(str(e))
    return device_infos


def get_remote_configs(config_url, logger_handler, cur_configs=None):
    remote_configs = {}
    try:
        device_infos = get_all_device_infos()
        logger_handler.info(json.dumps(device_infos))
        r_data = {'name': __DEVIDE_NAME, 'device_info': json.dumps(device_infos), 'version': __VERSION,
                  'time': time.time()}
        if cur_configs:
            r_data['cur_configs'] = json.dumps(cur_configs)
        req = requests.post(config_url, params=r_data)
        req.encoding = 'utf-8'
        rep_str = req.text
        logger_handler.info(rep_str)
        remote_configs = json.loads(rep_str)['data']
    except Exception as e:
        logger_handler.error(str(e))
    return remote_configs


def update_app(new_version_url, new_version, logger_handler):
    try:
        app_path = sys.argv[0]
        app_path_new = app_path + '.new'
        app_path_old = app_path + '.old'
        if os.path.exists(app_path_old):
            os.remove(app_path_old)
        if os.path.exists(app_path_new):
            os.remove(app_path_new)
        urllib.urlretrieve(new_version_url, app_path_new)
        if not os.path.exists(app_path_new):
            logger_handler.error('download new version fail ' + new_version_url)
            return
        st = os.stat(app_path_new)
        os.chmod(app_path_new, st.st_mode | stat.S_IXUSR | stat.S_IXGRP)
        c_version = commands.getoutput(app_path_new + ' -V')
        if not c_version or c_version.strip() != new_version:
            logger_handler.error("new version program version error")
            return
        os.rename(app_path, app_path_old)
        os.rename(app_path_new, app_path)
        logger_handler.info("change version suss")
    except Exception as e:
        logger_handler.error(str(e))


def check_update(g_config, logger_handler):
    ret = False
    if not g_config.get('config_url'):
        return ret

    try:
        remote_configs = get_remote_configs(g_config['config_url'], logger_handler, g_config)
        if not remote_configs:
            return ret
        remote_version = remote_configs.get('version')
        if remote_version and remote_version != __VERSION:
            if remote_configs.get('app_url'):
                update_app(remote_configs['app_url'], remote_version, logger_handler)
            return True

        for c_key, c_value in remote_configs.items():
            old_value = g_config.get(c_key)
            if old_value is not None and old_value != c_value:
                return True
    except Exception as e:
        logger_handler.error(str(e))

    return ret


def clean_pyinstaller_tmpfiles(logger_handler):
    try:
        for path in sys.path:
            cur_path_name = os.path.basename(path)
            cur_path = os.path.dirname(path)
            logger_handler.info('clean_tmpfiles ' + path)
            if re.match(r'^_MEI.+$', cur_path_name):
                for f in os.listdir(os.path.dirname(path)):
                    try:
                        check_path = cur_path + '/' + f.strip()
                        if cur_path_name == os.path.basename(check_path):
                            continue
                        if re.match(r'^_MEI.+$', os.path.basename(check_path)):
                            logger_handler.info('clean_tmpfiles ' + check_path)
                            if os.path.exists(check_path):
                                logger_handler.info('clean_tmpfiles rm ' + check_path)
                                shutil.rmtree(check_path)
                    except Exception as e:
                        logger_handler.error(str(e))
    except Exception as e:
        logger_handler.error(str(e))


def kill_old_app(logger_handler):
    try:
        app_name = sys.argv[0].split('/')[-1]
        pids_str = commands.getoutput('pidof ' + app_name)
        pids = pids_str.split()
        cid = os.getpid()
        ppid = os.getppid()
        logger_handler.info(str(cid) + ' ' + str(ppid))
        for pid in pids:
            if not pid or int(pid) == cid or int(pid) == ppid:
                continue
            logger_handler.info('kill ' + str(pid))
            os.kill(int(pid), signal.SIGINT)
    except Exception as e:
        logger_handler.error(str(e))


'''
/* Command strings */
#define GET_CAN_EXPAND  "sudo raspi-config nonint get_can_expand"
#define EXPAND_FS       "sudo raspi-config nonint do_expand_rootfs"
#define GET_HOSTNAME    "sudo raspi-config nonint get_hostname"
#define SET_HOSTNAME    "sudo raspi-config nonint do_hostname %s"
#define GET_BOOT_CLI    "sudo raspi-config nonint get_boot_cli"
#define GET_AUTOLOGIN   "sudo raspi-config nonint get_autologin"
#define SET_BOOT_CLI    "sudo raspi-config nonint do_boot_behaviour B1"
#define SET_BOOT_CLIA   "sudo raspi-config nonint do_boot_behaviour B2"
#define SET_BOOT_GUI    "sudo raspi-config nonint do_boot_behaviour B3"
#define SET_BOOT_GUIA   "sudo raspi-config nonint do_boot_behaviour B4"
#define GET_BOOT_WAIT   "sudo raspi-config nonint get_boot_wait"
#define SET_BOOT_WAIT   "sudo raspi-config nonint do_boot_wait %d"
#define GET_SPLASH      "sudo raspi-config nonint get_boot_splash"
#define SET_SPLASH      "sudo raspi-config nonint do_boot_splash %d"
#define GET_OVERSCAN    "sudo raspi-config nonint get_overscan"
#define SET_OVERSCAN    "sudo raspi-config nonint do_overscan %d"
#define GET_CAMERA      "sudo raspi-config nonint get_camera"
#define SET_CAMERA      "sudo raspi-config nonint do_camera %d"
#define GET_SSH         "sudo raspi-config nonint get_ssh"
#define SET_SSH         "sudo raspi-config nonint do_ssh %d"
#define GET_VNC         "sudo raspi-config nonint get_vnc"
#define SET_VNC         "sudo raspi-config nonint do_vnc %d"
#define GET_SPI         "sudo raspi-config nonint get_spi"
#define SET_SPI         "sudo raspi-config nonint do_spi %d"
#define GET_I2C         "sudo raspi-config nonint get_i2c"
#define SET_I2C         "sudo raspi-config nonint do_i2c %d"
#define GET_SERIAL      "sudo raspi-config nonint get_serial"
#define GET_SERIALHW    "sudo raspi-config nonint get_serial_hw"
#define SET_SERIAL      "sudo raspi-config nonint do_serial %d"
#define GET_1WIRE       "sudo raspi-config nonint get_onewire"
#define SET_1WIRE       "sudo raspi-config nonint do_onewire %d"
#define GET_RGPIO       "sudo raspi-config nonint get_rgpio"
#define SET_RGPIO       "sudo raspi-config nonint do_rgpio %d"
#define GET_PI_TYPE     "sudo raspi-config nonint get_pi_type"
#define GET_OVERCLOCK   "sudo raspi-config nonint get_config_var arm_freq /boot/config.txt"
#define SET_OVERCLOCK   "sudo raspi-config nonint do_overclock %s"
#define GET_GPU_MEM     "sudo raspi-config nonint get_config_var gpu_mem /boot/config.txt"
#define GET_GPU_MEM_256 "sudo raspi-config nonint get_config_var gpu_mem_256 /boot/config.txt"
#define GET_GPU_MEM_512 "sudo raspi-config nonint get_config_var gpu_mem_512 /boot/config.txt"
#define GET_GPU_MEM_1K  "sudo raspi-config nonint get_config_var gpu_mem_1024 /boot/config.txt"
#define SET_GPU_MEM     "sudo raspi-config nonint do_memory_split %d"
#define GET_HDMI_GROUP  "sudo raspi-config nonint get_config_var hdmi_group /boot/config.txt"
#define GET_HDMI_MODE   "sudo raspi-config nonint get_config_var hdmi_mode /boot/config.txt"
#define SET_HDMI_GP_MOD "sudo raspi-config nonint do_resolution %d %d"
#define GET_WIFI_CTRY   "sudo raspi-config nonint get_wifi_country"
#define SET_WIFI_CTRY   "sudo raspi-config nonint do_wifi_country %s"
#define CHANGE_PASSWD   "(echo \"%s\" ; echo \"%s\" ; echo \"%s\") | passwd"
'''


def init_device(logger_handler):
    try:
        device_infos = get_all_device_infos()
        if device_infos and device_infos['disk']['total'] > 6 * 1024 * 1024 * 1024:
            logger_handler.info('init done')
            return

        init_flag_num = 0
        if os.path.exists(__INIT_FLAG_FILE):
            try:
                init_flag_info = open(__INIT_FLAG_FILE).read()
                init_flag_num = int(init_flag_info)
            except Exception as e:
                logger_handler.error(str(e))

        if init_flag_num > 3:
            logger_handler.error('init num ' + str(init_flag_num))
            return

        init_flag_num += 1
        try:
            out = open(__INIT_FLAG_FILE, 'w')
            out.write(str(init_flag_num))
            out.close()
        except Exception as e:
            logger_handler.error(str(e))

        try:
            if os.path.exists(__DEVIDE_NAME_FILE):
                device_name = ''
                with open(__DEVIDE_NAME_FILE) as d_file:
                    device_name = d_file.read().strip()
                hostname = commands.getoutput('sudo raspi-config nonint get_hostname')
                if hostname and device_name and hostname.strip() != device_name:
                    os.system('sudo raspi-config nonint do_hostname %s' % device_name)
                    logger_handler.info('set hostname %s' % device_name)
        except Exception as e:
            logger_handler.error(str(e))

        os.system('sudo raspi-config nonint do_expand_rootfs')
        os.system('reboot')
    except Exception as e:
        logger_handler.error(str(e))


def start_service():
    device_infos = get_all_device_infos()
    g_config = parse_args()
    init_log(g_config)
    logger_handler = logging.getLogger('device_record')
    logger_handler.info(json.dumps(g_config))

    init_device(logger_handler)

    # get device name
    try:
        global __DEVIDE_NAME
        if os.path.exists(__DEVIDE_NAME_FILE):
            with open(__DEVIDE_NAME_FILE) as d_file:
                d_name = d_file.read().strip()
                __DEVIDE_NAME = d_name
                logger_handler.info('device name: ' + __DEVIDE_NAME)
    except Exception as e:
        logger_handler.error(str(e))

    # merge remote configs
    try:
        if g_config.get('config_url'):
            remote_configs = get_remote_configs(g_config['config_url'], logger_handler, g_config)
            if remote_configs:
                for c_key, c_value in remote_configs.items():
                    if c_value:
                        g_config[c_key] = c_value
    except Exception as e:
        logger_handler.error(str(e))

    kill_old_app(logger_handler)
    clean_pyinstaller_tmpfiles(logger_handler)

    ip, mac = '', ''
    if device_infos and device_infos.get('net_addrs'):
        ech0_info = device_infos['net_addrs'].get('eth0')
        wlan0_info = device_infos['net_addrs'].get('wlan0')
        if ech0_info and ech0_info.get('ip'):
            ip = ech0_info['ip']
            mac = ech0_info['mac']
        if not ip and wlan0_info and wlan0_info.get('ip'):
            ip = wlan0_info['ip']
            mac = wlan0_info['mac']

    stream_list = []
    worker_list = []
    for i in range(1, 5):
        stream_info = {'url': '', 'usb_id': i, 'ip': ip, 'mac': mac, 'device_name': __DEVIDE_NAME}
        stream_list.append(stream_info)
        w = DeviceRecorder(stream_info, g_config)
        w.start()
        worker_list.append(w)

    check_update_interval_s = g_config.get('check_update_interval_s', 600)
    while True:
        try:
            if check_update(g_config, logger_handler):
                logger_handler.error('restart app')
                sys.exit(1)
            time.sleep(check_update_interval_s)
            logger_handler.info('check_update')
        except Exception as e:
            logger_handler.error(str(e))


if __name__ == '__main__':
    start_service()


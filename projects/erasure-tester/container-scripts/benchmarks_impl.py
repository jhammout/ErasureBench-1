#!/usr/bin/env python3
import random
import re
import string
import subprocess
from datetime import datetime

from redis_cluster import RedisCluster
from time import sleep
from utils import kill_pid


class BenchmarksImpl:
    """
    Collection of benchmarks to run against the filesystem. Each method defined in this class will be executed as a
    benchmark, provided that its name begins with bench_. Each method takes a single parameter in the form of a tuple.
    It contains the configuration in which the test is running, like this:
    (Erasure code name, Generator for the number of nodes in the Redis cluster, Storage backend name, Stripe size, Parity size, SRC size)

    Each bench_ method must return a dict formed like the following:
    {
        'name of metric 1': 1234,
        'name of metric 2': 9876,
    }
    """

    def __init__(self, mountpoint):
        self.mount = mountpoint

    def generate_file_name(self):
        return self.mount + ''.join(random.choice(string.ascii_letters) for _ in range(12))

    @staticmethod
    def _convert_to_kb(value, unit):
        if unit.startswith('M'):
            return float(value) * 1000.0
        elif unit.startswith('k'):
            return float(value)
        else:
            raise Exception('Unit not supported, please complete the _convert_to_kb method')

    def _bench_checksum(self, config, redis: RedisCluster, java, nodes_trace, archive_name, sha_name, tar_log_lines, sha_log_lines):
        print('Uncompressing archive (%s)...' % archive_name)
        tar_proc = subprocess.Popen(['tar', '-xvf', '/opt/erasuretester/%s' % archive_name, '-C', self.mount],
                                    stdout=subprocess.PIPE, bufsize=1)
        self._show_subprocess_percent(tar_proc, tar_log_lines)

        results = dict()
        for redis_size in (x[0] for x in nodes_trace):
            redis.scale(redis_size)
            print('Flushing read cache...')
            java.flush_read_cache()
            print('Waiting 60 seconds for things to stabilize...')
            sleep(60)

            print('Checking files...')
            sha_proc = subprocess.Popen(
                ['sha256sum', '-c', '/opt/erasuretester/%s' % sha_name],
                stdout=subprocess.PIPE, bufsize=1)
            sha_output = self._show_subprocess_percent(sha_proc, sha_log_lines)
            ok_files = len([x for x in sha_output if b' OK' in x])
            failed_files = len([x for x in sha_output if b' FAILED' in x])
            print('   Checked. %d correct, %d failed' % (ok_files, failed_files))
            inter_results = {
                'RS0': config[1],
                'RS': redis.cluster_size,
                'OK Files': ok_files,
                'Failed files': failed_files,
                'Failure ratio': failed_files / (ok_files + failed_files),
            }
            results['RS=%d' % redis.cluster_size] = inter_results

            if ok_files == 0:  # It's no use to continue
                break
        return results

    def bench_apache(self, config, redis: RedisCluster, java, nodes_trace):
        return self._bench_checksum(config, redis, java, nodes_trace,
                                    'httpd.tar.bz2', 'httpd.sha256', tar_log_lines=2614, sha_log_lines=2517)

    def bench_bc(self, config, redis: RedisCluster, java, nodes_trace):
        return self._bench_checksum(config, redis, java, nodes_trace,
                                    'bc.tar.gz', 'bc.sha256', tar_log_lines=94, sha_log_lines=94)

    def bench_10bytes(self, config, redis: RedisCluster, java, nodes_trace):
        return self._bench_checksum(config, redis, java, nodes_trace,
                                    '10bytes.tar.bz2', '10bytes.sha256', tar_log_lines=1001, sha_log_lines=1000)

    def bench_net_throughput(self, config, redis: RedisCluster, java, nodes_trace):
        dumpcap = ['/usr/bin/dumpcap', '-q', '-i', 'any', '-f', 'tcp port 6379', '-s', '64', '-w']

        print("Starting dumpcap")
        isoformat = datetime.today().isoformat()
        capture_dir = '/opt/erasuretester/results/'
        write_capture_file = 'capture_%s_%s_write.pcapng' % (isoformat, config[0])
        sleep(5)
        dumpcap_proc = subprocess.Popen(dumpcap + [capture_dir + write_capture_file])
        sleep(10)
        print("Uncompressing archive")
        tar_proc = subprocess.Popen(['tar', '-xvf', '/opt/erasuretester/httpd.tar.bz2', '-C', self.mount],
                                    stdout=subprocess.PIPE, bufsize=1)
        self._show_subprocess_percent(tar_proc, 2614)
        sleep(10)
        kill_pid(dumpcap_proc)
        subprocess.check_call(['chmod', '666', capture_dir + write_capture_file])

        measures = []
        for redis_size in (x[0] for x in nodes_trace):
            redis.scale(redis_size)
            capture_file = 'capture_%s_%s_read_%d.pcapng' % (isoformat, config[0], redis_size)

            dumpcap_proc = subprocess.Popen(dumpcap + [capture_dir + capture_file])
            sleep(10)
            print('Checking files...')
            sha_proc = subprocess.Popen(
                ['sha256sum', '-c', '/opt/erasuretester/httpd.sha256'],
                stdout=subprocess.PIPE, bufsize=1)
            sha_output = self._show_subprocess_percent(sha_proc, 2517)
            ok_files = len([x for x in sha_output if b' OK' in x])
            failed_files = len([x for x in sha_output if b' FAILED' in x])

            sleep(10)
            kill_pid(dumpcap_proc)
            subprocess.check_call(['chmod', '666', capture_dir + capture_file])

            measures.append({
                'ok': ok_files,
                'failed': failed_files,
                'capture': capture_file,
                'redis_initial': config[1],
                'redis_current': redis_size
            })

        return {
            'write_capture': write_capture_file,
            'measures': measures
        }

    @staticmethod
    def _show_subprocess_percent(proc, expected_nb_lines):
        percent = 0
        lines = []
        with proc.stdout:
            for line in iter(proc.stdout.readline, b''):
                lines.append(line)
                new_percent = int(len(lines) / expected_nb_lines * 100)
                if new_percent != percent and new_percent % 5 == 0:
                    percent = new_percent
                    print("%d %%" % percent)
        proc.wait()
        return lines

    def bench_kill(self, config, redis: RedisCluster, java, nodes_trace):
        if config[1] < 2 or config[0] == 'Null':
            # The benchmark would crash needlessly
            return {}

        self.bench_dd(block_count=20)
        for redis_size in (x[0] for x in nodes_trace):
            redis.scale(redis_size)
            java.flush_read_cache()
            self.bench_dd(block_count=20)
        return {}

    def bench_dd(self, config=None, redis=None, java=None, nodes_trace=None, block_count=50):
        write_speed = read_speed = 0

        for _ in range(3):
            filename = self.generate_file_name()
            out = subprocess.check_output(("dd if=/dev/zero of=%s bs=128kB count=%d" % (filename, block_count))
                                          .split(' '), stderr=subprocess.STDOUT, universal_newlines=True)
            match = re.search(r'([0-9.]+) ([a-zA-Z]?B/s)$', out)
            write_speed = max(self._convert_to_kb(*match.groups()), write_speed)

            out = subprocess.check_output(("dd if=%s of=/dev/null bs=128kB count=%d" % (filename, block_count))
                                          .split(' '), stderr=subprocess.STDOUT, universal_newlines=True)
            match = re.search(r'([0-9.]+) ([a-zA-Z]?B/s)$', out)
            read_speed = max(self._convert_to_kb(*match.groups()), read_speed)

        return {
            'read': read_speed,
            'write': write_speed
        }

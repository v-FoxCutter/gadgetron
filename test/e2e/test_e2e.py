#!/usr/bin/python3

import pytest
import os
import socket
import hashlib
import subprocess
import yaml
import time
import string
import re
import sys

import h5py
import ismrmrd
import numpy
import glob
import json

import urllib.error
import urllib.request

from pathlib import Path


# Importing h5py on windows will mess with your environment. When we pass the messed up environment to gadgetron
# child processes, they won't load properly. We're saving our environment here to spare our children from the
# crimes of h5py.
environment = dict(os.environ)


# http://gadgetrondata.blob.core.windows.net/gadgetrontestdata/

def calc_mdf5(file):
    md5 = hashlib.new('md5')
    
    with open(file, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            md5.update(chunk)
    return md5.hexdigest()

def is_valid(file, digest):
    if not os.path.isfile(file):
        return False

    return digest == calc_mdf5(file) 

def urlretrieve(url, filename, retries=5):
    if retries <= 0:
        raise RuntimeError("Download from {} failed".format(url))
    try:
        with urllib.request.urlopen(url, timeout=60) as connection:                        
            with open(filename,'wb') as f:
                for chunk in iter(lambda : connection.read(1024*1024), b''):
                    f.write(chunk)
            return connection.headers["Content-MD5"]
    except (urllib.error.URLError, ConnectionResetError, socket.timeout) as exc:
        print("Retrying connection for file {}, reason: {}".format(filename, str(exc)))
        urlretrieve(url, filename, retries=retries-1)

@pytest.fixture
def fetch_data_file(cache_path: Path, data_host_url:str, cache_disable:bool, tmp_path: Path):
    created_files = []
    
    def _fetch_data_file(filename:str, fileHash:str):
        nonlocal created_files
        url = "{}{}".format(data_host_url, filename)

        if cache_disable:
            destination = os.path.join(tmp_path, filename)
        else:
            destination = os.path.join(cache_path, filename)

        if not os.path.exists(destination):
            print("Downloading file: {}".format(url)) 

            os.makedirs(os.path.dirname(destination), exist_ok=True)
            urlretrieve(url, destination)
            
        else:
            print("File already exists: {}".format(destination)) 

        if not is_valid(destination, fileHash):
            raise(RuntimeError("Downloaded file {} failed validation. Expected MD5 {}. Actual MD5 {}".format(destination, fileHash, calc_mdf5(destination))))

        created_files.append(destination)

        return destination
    
    yield _fetch_data_file

    if cache_disable:
        for file in created_files:
            if os.path.isfile(file):
                os.remove(file)
                if len(os.listdir(os.path.dirname(file))) == 0:
                    os.removedirs(os.path.dirname(file))

@pytest.fixture
def siemens_to_ismrmrd(tmp_path: Path, fetch_data_file):
    def _siemens_to_ismrmrd(fileConfig:dict, section:str):
        if 'copy_file' in fileConfig:
            output = fetch_data_file(fileConfig['copy_file'], fileConfig['copy_file_hash'])
            print("Using output file {}".format(output)) 

        else:
            input = fetch_data_file(fileConfig['data_file'], fileConfig['data_file_hash'])
            output = os.path.join(tmp_path, os.path.basename(input) + "_" + section + ".h5")

            print("Convert file {} to {}".format(input, output)) 

            command = ["siemens_to_ismrmrd", "-X",
                    "-f", input, 
                    "-m", fileConfig.get('parameter_xml', 'IsmrmrdParameterMap_Siemens.xml'),
                    "-x", fileConfig.get('parameter_xsl', 'IsmrmrdParameterMap_Siemens.xsl'),
                    "-o", output,
                    "-z", str(fileConfig['measurement']),
                    fileConfig.get('data_conversion_flag', '')]

            with open(os.path.join(tmp_path, os.path.basename(input) + "_" + section + '.log.out'), 'w') as log_stdout:
                with open(os.path.join(tmp_path, os.path.basename(input) + "_" + section + '.log.err'), 'w') as log_stderr:
                    result = subprocess.run(command,
                                stdout=log_stdout,
                                stderr=log_stderr, 
                                cwd=tmp_path)
            
            if result.returncode != 0:
                pytest.fail("siemens_to_ismrmrd failed with return code {}".format(result.returncode))

        return output
    
    return _siemens_to_ismrmrd


reqs = {
    'python_support': 'python',
    'julia_support': 'julia',
    'matlab_support': 'matlab',
    'system_memory': 'memory',
    'gpu_support': 'cuda',
    'gpu_memory': 'cuda'
}

def query_gadgetron_capabilities(info_string):
    print("Querying Gadgetron capabilities...")

    value_pattern = r"(?:\s*):(?:\s+)(?P<value>.*)?"

    capability_markers = {
        'version': "Version",
        'build': "Git SHA1",
        'memory': "System Memory size",
        'python': "Python Support",
        'julia': "Julia Support",
        'matlab': "Matlab Support",
        'cuda': "CUDA Support",
    }

    plural_capability_markers = {
        'cuda_memory': "Total amount of global GPU memory"
    }

    def find_value(marker):
        pattern = re.compile(marker + value_pattern, re.IGNORECASE)
        match = pattern.search(info_string)

        if not match:
            raise RuntimeError("Failed to parse Gadgetron info string; Gadgetron capabilities could not be determined.")

        return match['value']

    def find_plural_values(marker):
        pattern = re.compile(marker + value_pattern, re.IGNORECASE)
        return [match['value'] for match in pattern.finditer(info_string)]

    capabilities = {key: find_value(marker) for key, marker in capability_markers.items()}
    capabilities.update({key: find_plural_values(marker) for key, marker in plural_capability_markers.items()})

    return capabilities

@pytest.fixture
def check_requirements(host_url, port, external, ignore_requirements, run_tag):
    def _check_requirements(requirements:dict, tags:list):
        def rules_from_reqs(section):
            class Rule:
                def __init__(self, capability, validator, message):
                    self.capability = capability
                    self.validator = validator
                    self.message = message

                def is_satisfied(self, capabilities):
                    value = capabilities.get(self.capability)
                    return self.validator(value)
        
            def parse_memory(string):
                pattern = re.compile(r"(?P<value>\d+)(?: MB)?")
                match = pattern.search(string)
                return float(match['value'])

            def is_enabled(value):
                return value in ['YES', 'yes', 'True', 'true', '1']

            def has_more_than(target):
                return lambda value: parse_memory(str(target)) <= parse_memory(value)

            def each(validator):
                return lambda values: all([validator(value) for value in values])

            rules = [
                ('matlab_support', lambda req: Rule('matlab', is_enabled, "MATLAB support required.")),
                ('python_support', lambda req: Rule('python', is_enabled, "Python support required.")),
                ('julia_support', lambda req: Rule('julia', is_enabled, "Julia support required.")),
                ('system_memory', lambda req: Rule('memory', has_more_than(req), "Not enough system memory.")),
                ('gpu_support', lambda req: Rule('cuda', is_enabled, "CUDA support required.")),
                ('gpu_memory', lambda req: Rule('cuda_memory', each(has_more_than(req)), "Not enough graphics memory."))
            ]

            return [(key, rule(section[key])) for key, rule in rules if key in section]

        if run_tag != "" and run_tag not in tags:
            pytest.skip("Test missing required tag.")    

        if 'skip' in tags:
            pytest.skip("Test was marked as skipped")

        if ignore_requirements:
            return

        if external: 
            command = ["gadgetron", "--info"]
        else:
            command = ["gadgetron_ismrmrd_client",
                        "-a", host_url,
                        "-p", str(port),
                        "-q", "-Q", "gadgetron::info"]

        info_string = subprocess.check_output(command, stderr=subprocess.STDOUT, universal_newlines=True)
        capabilities = query_gadgetron_capabilities(info_string)

        reqs = rules_from_reqs(requirements)

        for rule in [rule for key, rule in reqs]:
            if not rule.is_satisfied(capabilities):
                pytest.skip(rule.message)    

    return _check_requirements        

template_path = './config'


@pytest.fixture
def send_to_gadgetron(tmp_path: Path, host_url, port):
    def _send_to_gadgetron(fileConfig:dict, input_file:str, section:str):
        output_file = os.path.join(tmp_path, section + ".output.mrd")
        additional_arguments = fileConfig.get('additional_arguments', '')

        if 'template' in fileConfig:
            template_file = os.path.join(template_path, fileConfig['template'])
            configuration = os.path.join(tmp_path, section + '.config.xml')

            config_option = ["-G", section, 
                            "-C", configuration]

            with open(template_file, 'r') as input:
                with open(configuration, 'w') as output:
                    output.write(
                        string.Template(input.read()).substitute(
                            test_folder=os.path.abspath(tmp_path),
                            # Expand substitution list as needed.
                        )
                    )

        else:
            configuration = fileConfig['configuration']

            config_option = ["-G", configuration, 
                            "-c", configuration]

        
        print("Passing data to Gadgetron: {} -> {}".format(input_file, output_file)) 
        command = ["gadgetron_ismrmrd_client",
                    "-a", host_url,
                    "-p", port,
                    "-f", input_file,
                    "-o", output_file]
        
        command += config_option

        if additional_arguments:
            command = command + additional_arguments.split()

        with open(os.path.join(tmp_path, section + '_gadgetron.log.out'), 'w') as log_stdout:
            with open(os.path.join(tmp_path, section + '_gadgetron.log.err'), 'w') as log_stderr:
                result = subprocess.run(command,
                            stdout=log_stdout,
                            stderr=log_stderr, 
                            env=environment)

        if result.returncode != 0:
            pytest.fail("gadgetron_ismrmrd_client failed with return code {}".format(result.returncode))

        # assert os.path.isfile(output_file), "{} is missing".format(output_file)

        return output_file

    return _send_to_gadgetron

@pytest.fixture
def stream_to_gadgetron(tmp_path: Path, host_url, port, storage_port):
    def _stream_to_gadgetron(fileConfig:dict, input_file:str, section:str):
        output_file = os.path.join(tmp_path, section + ".output.mrd")
        storage_address = "http://localhost:" + str(storage_port)

        input_adapter = fileConfig['input_adapter']
        output_adapter = fileConfig['output_adapter']
        output_group = fileConfig['output_group']
        streamConfig = fileConfig['stream']

        stream_command = f"{input_adapter} -i {input_file} --use-stdout"

        for stream in streamConfig:
            stream_command += f" | gadgetron -E {storage_address} --from_stream -c {stream['configuration']} {stream.get('args', '')}"
            
        stream_command += f" | {output_adapter} --use-stdin -o {output_file} -g {output_group}"

        # Some stream arguments use ${test_folder} directly so this will provide support for that.
        stream_command = stream_command.replace('${test_folder}', os.path.abspath(tmp_path))

        split_cmd = ['bash', '-c', stream_command]

        with open(os.path.join(tmp_path, section + '_gadgetron.log.out'), 'w') as log_stdout:
            with open(os.path.join(tmp_path, section + '_gadgetron.log.err'), 'w') as log_stderr:
                result = subprocess.run(split_cmd,
                            env=environment,
                            stdout=log_stdout,
                            stderr=log_stderr)

        if result.returncode != 0:
            pytest.fail("stream processing failed with return code {}".format(result.returncode))

        # assert os.path.isfile(output_file), "{} is missing".format(output_file)

        return output_file

    return _stream_to_gadgetron

def wait_for_storage_server(port, proc, retries=50):
    for i in range(retries):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/healthcheck")
            return
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            if i == retries - 1 or proc.poll() is not None:
                raise RuntimeError("Unable to get a successful response from storage server.") from e
            time.sleep(0.2)

def start_storage_server(*, log, port, storage_folder):
    storage_server_environment = environment.copy()
    storage_server_environment["MRD_STORAGE_SERVER_PORT"] = port
    storage_server_environment["MRD_STORAGE_SERVER_STORAGE_CONNECTION_STRING"] = storage_folder
    storage_server_environment["MRD_STORAGE_SERVER_DATABASE_CONNECTION_STRING"] = storage_folder + "/metadata.db"

    retries = 5
    for i in range(retries):
        print("Starting MRD Storage Server on port", port)
        proc = subprocess.Popen(["mrd-storage-server", "--require-parent-pid", str(os.getpid())],
                                stdout=log,
                                stderr=log,
                                env=storage_server_environment)

        try:
            wait_for_storage_server(port, proc)
            return proc
        except:
            # If the process has exited, it might be because the
            # port was in use. This can be because the previous storage server
            # instance was just killed. So we try again.
            if proc.poll() is not None and i < retries:
                time.sleep(1)
            else:
                proc.kill()
                raise

def start_gadgetron_instance(*, log_stdout, log_stderr, port, storage_address, env=environment):
    print("Starting Gadgetron instance on port", port)
    proc = subprocess.Popen(["gadgetron", "-p", port, "-E", storage_address],
                            stdout=log_stdout,
                            stderr=log_stderr,
                            env=env)
    return proc


@pytest.fixture(autouse="true")
def start_storage(external, storage_port, tmp_path_factory):
    log_path = tmp_path_factory.mktemp("logs")
    storage_path = tmp_path_factory.mktemp("storage")

    if not external:
        print("Starting storage server on port", storage_port)
        with open(os.path.join(log_path, 'storage.log'), 'w') as log:
            storage = start_storage_server(log=log,
                        port=str(storage_port),
                        storage_folder=str(storage_path))
            
            yield

            storage.kill()

    else:
        return

@pytest.fixture
def start_gadgetron(port, external, storage_port, tmp_path):
    instance = None
    def _start_gadgetron():
        nonlocal instance

        storage_address = "http://localhost:" + storage_port

        if not external:
            print("Starting Gadgetron instance on port {} with logs in {}".format(port, tmp_path))

            with open(os.path.join(tmp_path, 'gadgetron.log.out'), 'w') as log_stdout:
                with open(os.path.join(tmp_path, 'gadgetron.log.err'), 'w') as log_stderr:
                    instance = start_gadgetron_instance(log_stdout=log_stdout, log_stderr=log_stderr, port=port, storage_address=storage_address)


        else:
            yield


    yield _start_gadgetron

    if instance != None:
        instance.kill()


@pytest.fixture
def start_gadgetron_with_additional_nodes(tmp_path: Path, port, storage_port, external):
    instances = []

    def _start_gadgetron_with_additional_nodes(fileConfig:dict):
        nonlocal instances

        if external:
            return

        if 'nodes' not in fileConfig:
            return

        storage_address = "http://localhost:" + storage_port
        env=environment
        base_port = int(fileConfig.get('node_port_base', 9050))
        number_of_nodes = int(fileConfig['nodes'])

        ids = range(number_of_nodes)
        print("Will start additional Gadgetron workers on ports:", *map(lambda idx: base_port + idx, ids))

        worker_list = []

        for id in ids:
            instance_port = str(base_port + id)
            with open(os.path.join(tmp_path, 'gadgetron_worker' + instance_port + '.log.out'), 'w') as log_stdout:
                with open(os.path.join(tmp_path, 'gadgetron_worker' + instance_port + '.log.err'), 'w') as log_stderr:
                    instances.append(start_gadgetron_instance(log_stdout=log_stdout, log_stderr=log_stderr, port=instance_port, storage_address=storage_address))
                    worker_list=worker_list + ['localhost:' + str(instance_port)]

        if sys.platform.startswith('win32'):
            env['GADGETRON_REMOTE_WORKER_COMMAND'] = 'cmd /k echo ' + json.dumps(worker_list) + ' & exit'
        else:
            env["GADGETRON_REMOTE_WORKER_COMMAND"] = "echo " + json.dumps(worker_list)

        print("Setting env to", env["GADGETRON_REMOTE_WORKER_COMMAND"])

        print("Starting main Gadgetron instance on port {} with logs in {}".format(port, tmp_path))

        with open(os.path.join(tmp_path, 'gadgetron.log.out'), 'w') as log_stdout:
            with open(os.path.join(tmp_path, 'gadgetron.log.err'), 'w') as log_stderr:
                instances.append(start_gadgetron_instance(log_stdout=log_stdout, log_stderr=log_stderr, port=port, storage_address=storage_address, env=env))

    yield _start_gadgetron_with_additional_nodes

    for instance in instances:
        instance.kill()


Failure = "Failure", 1

def validate_output(*, output_file, reference_file, output_group, reference_group, value_threshold, scale_threshold):
    try:
        # The errors produced by h5py are not entirely excellent. We spend some code here to clear them up a bit.
        def get_group_data(file, group):
            with h5py.File(file, mode='r') as f:
                try:
                    group = group + '/data'
                    return numpy.squeeze(f[group])
                except KeyError:
                    raise RuntimeError("Did not find group '{}' in file {}".format(group, file))

        output_data = get_group_data(output_file, output_group)
        reference_data = get_group_data(reference_file, reference_group)
    except OSError as e:
        return Failure, str(e)
    except RuntimeError as e:
        return Failure, str(e)

    output = output_data[...].flatten().astype('float32')
    reference = reference_data[...].flatten().astype('float32')

    norm_diff = numpy.linalg.norm(output - reference) / numpy.linalg.norm(reference)
    scale = numpy.dot(output, output) / numpy.dot(output, reference)

    if value_threshold < norm_diff:
        return Failure, "Comparing values, norm diff: {} (threshold: {})".format(norm_diff, value_threshold)

    if value_threshold < abs(1 - scale):
        return Failure, "Comparing image scales, ratio: {} ({}) (threshold: {})".format(scale, abs(1 - scale),
                                                                                        scale_threshold)

    return None, "Norm: {:.1e} [{}] Scale: {:.1e} [{}]".format(norm_diff, value_threshold, abs(1 - scale),
                                                                scale_threshold)


def validate_dataset(*, dataset_file, reference_file, dataset_group, reference_group):
    try:
        dataset_file = ismrmrd.File(dataset_file, 'r')
    except OSError as e:
        return Failure, "Failed to read dataset file '{}'".format(dataset_file)

    try:
        reference_file = ismrmrd.File(reference_file, 'r')
    except OSError as e:
        return Failure, "Failed to read reference file '{}'".format(reference_file)

    header = dataset_file[dataset_group].header
    ref_header = reference_file[reference_group].header
    if not dataset_file[dataset_group].header == reference_file[reference_group].header:
        import deepdiff
        diff = deepdiff.diff.DeepDiff(header, ref_header)
        print(diff.pretty())
        return Failure, "Dataset header did not match reference header"

    for attribute in ['acquisitions', 'waveforms', 'images']:

        dataset = getattr(dataset_file[dataset_group], attribute) or []
        reference = getattr(reference_file[reference_group], attribute) or []

        if not list(dataset) == list(reference):
            return Failure, "Dataset {attr} did not match reference {attr}".format(attr=attribute)

    return None, "Dataset matched reference"

@pytest.fixture
def validate_dataset_output(tmp_path: Path, fetch_data_file):
    def _validate_dataset_output(fileConfig:dict):
        
        dataset_prefix = os.path.join(tmp_path, fileConfig['dataset_prefix'])
        dataset_files = glob.glob(dataset_prefix + "*")

        print(dataset_files)

        if len(dataset_files) == 0:
            return Failure, "Found no dataset with prefix: {}".format(dataset_prefix)

        if len(dataset_files) > 1:
            return Failure, "Too many datasets with prefix: {}".format(dataset_prefix)

        reference_file = fetch_data_file(fileConfig['reference_file'], fileConfig['reference_file_hash'])

        result, reason = validate_dataset(dataset_file=dataset_files[0],
                                            dataset_group=fileConfig.get('dataset_group', 'dataset'),
                                            reference_file=reference_file,
                                            reference_group=fileConfig.get('reference_group', 'dataset'))
        return result, reason

    return _validate_dataset_output

@pytest.fixture
def validate_output_data(fetch_data_file):
    def _validate_output_data(fileConfig:dict, output_file, output_image):
        reference_file = fetch_data_file(fileConfig['reference_file'], fileConfig['reference_file_hash'])
        result, reason = validate_output(output_file=output_file, 
                                        reference_file=reference_file, 
                                        output_group=output_image,
                                        reference_group=fileConfig['reference_image'], 
                                        value_threshold=fileConfig['value_comparison_threshold'], 
                                        scale_threshold=fileConfig['scale_comparison_threshold'])


        return result, reason

    return _validate_output_data



case_path = './cases'

def load_cases():    
    case_list = []
    
    for filename in os.listdir(case_path):
        if not filename.endswith(".yml"):
            continue
        
        with open(os.path.join(case_path, filename), 'r') as file:
            cases = yaml.safe_load(file)

            for case in cases['cases']:
                case_list.append(case)

    case_list.sort(key=lambda x: (x['name']))

    return case_list

def get_recontruction_cases(stream_cases, distributed_cases):
    cases = []
    case_ids = []
    
    for case in test_cases:
        if case not in stream_cases and case not in distributed_cases:
            cases.append(case)
            case_ids.append(case['name'])

    return cases, case_ids

def get_streaming_cases():
    cases = []
    case_ids = []
    
    for case in test_cases:
        if 'stream' in case['reconstruction']:    
            cases.append(case)
            case_ids.append(case['name'])

        elif 'dependency' in case and 'stream' in case['dependency']:
            cases.append(case)
            case_ids.append(case['name'])

    return cases, case_ids


def get_distributed_cases():
    cases = []
    case_ids = []
    
    for case in test_cases:
        if 'distributed' in case:
            cases.append(case)
            case_ids.append(case['name'])

    return cases, case_ids


test_cases = load_cases()

stream_cases, stream_ids = get_streaming_cases()
distributed_cases, distributed_ids = get_distributed_cases()
recontruction_cases, recontruction_ids = get_recontruction_cases(stream_cases, distributed_cases)


@pytest.mark.parametrize('config', recontruction_cases, ids=recontruction_ids)
def test_reconstruction(config, start_gadgetron, validate_output_data, validate_dataset_output, siemens_to_ismrmrd, send_to_gadgetron, check_requirements):
    start_gadgetron()

    check_requirements(config['requirements'], config['tags'])
        
    if 'dependency' in config:
        dependency_file = siemens_to_ismrmrd(config['dependency'], "dependency")
        _ = send_to_gadgetron(config['dependency'], dependency_file, "dependency")

    reconstruction_file = siemens_to_ismrmrd(config['reconstruction'], "reconstruction")
    output_file = send_to_gadgetron(config['reconstruction'], reconstruction_file, "reconstruction")

    if 'equals' in config['validation']:
        result, reason = validate_dataset_output(config['validation']['equals'])
        if result == Failure:
            pytest.fail(reason)

    for output_image in config['validation']['images']:   
        result, reason = validate_output_data(config['validation']['images'][output_image], output_file, output_image)             
        if result == Failure:
            pytest.fail(reason)

@pytest.mark.parametrize('config', stream_cases, ids=stream_ids)
def test_streaming(config, validate_output_data, validate_dataset_output, siemens_to_ismrmrd, stream_to_gadgetron, check_requirements):

    check_requirements(config['requirements'], config['tags'])

    if 'dependency' in config:
        dependency_file = siemens_to_ismrmrd(config['dependency'], "dependency")
        _ = stream_to_gadgetron(config['dependency'], dependency_file, "dependency")

    reconstruction_file = siemens_to_ismrmrd(config['reconstruction'], "reconstruction")
    output_file = stream_to_gadgetron(config['reconstruction'], reconstruction_file, "reconstruction")

    if 'equals' in config['validation']:
        result, reason = validate_dataset_output(config['validation']['equals'])
        if result == Failure:
            pytest.fail(reason)

    for output_image in config['validation']['images']:   
        result, reason = validate_output_data(config['validation']['images'][output_image], output_file, output_image)             
        if result == Failure:
            pytest.fail(reason)

@pytest.mark.parametrize('config', distributed_cases, ids=distributed_ids)
def test_distributed(config, start_gadgetron_with_additional_nodes, validate_output_data, validate_dataset_output, siemens_to_ismrmrd, send_to_gadgetron, check_requirements):
    start_gadgetron_with_additional_nodes(config['distributed'])

    check_requirements(config['requirements'], config['tags'])   

    if 'dependency' in config:
        dependency_file = siemens_to_ismrmrd(config['dependency'], "dependency")
        _ = send_to_gadgetron(config['dependency'], dependency_file, "dependency")

    reconstruction_file = siemens_to_ismrmrd(config['reconstruction'], "reconstruction")
    output_file = send_to_gadgetron(config['reconstruction'], reconstruction_file, "reconstruction")

    if 'equals' in config['validation']:
        result, reason = validate_dataset_output(config['validation']['equals'])
        if result == Failure:
            pytest.fail(reason)

    for output_image in config['validation']['images']:   
        result, reason = validate_output_data(config['validation']['images'][output_image], output_file, output_image)             
        if result == Failure:
            pytest.fail(reason)

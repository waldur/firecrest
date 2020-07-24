#
#  Copyright (c) 2019-2020, ETH Zurich. All rights reserved.
#
#  Please, refer to the LICENSE file in the root directory.
#  SPDX-License-Identifier: BSD-3-Clause
#
import logging
import os
import jwt
import stat
import tempfile
import json
import functools
from flask import request, jsonify
import requests
import urllib
import base64
import io

debug = os.environ.get("F7T_DEBUG_MODE", None)

AUTH_HEADER_NAME = 'Authorization'
realm_pubkey=os.environ.get("F7T_REALM_RSA_PUBLIC_KEY", '')
if realm_pubkey != '':
    # headers are inserted here, must not be present
    realm_pubkey = realm_pubkey.strip('\'"')   # remove '"'
    realm_pubkey = '-----BEGIN PUBLIC KEY-----\n' + realm_pubkey + '\n-----END PUBLIC KEY-----'
    realm_pubkey_type = os.environ.get("F7T_REALM_RSA_TYPE").strip('\'"')

AUTH_AUDIENCE = os.environ.get("F7T_AUTH_TOKEN_AUD", '').strip('\'"')
ALLOWED_USERS = os.environ.get("F7T_AUTH_ALLOWED_USERS", '').strip('\'"').split(";")
AUTH_REQUIRED_SCOPE = os.environ.get("F7T_AUTH_REQUIRED_SCOPE", '').strip('\'"')

AUTH_ROLE = os.environ.get("F7T_AUTH_ROLE", '').strip('\'"')


CERTIFICATOR_URL = os.environ.get("F7T_CERTIFICATOR_URL")
TASKS_URL = os.environ.get("F7T_TASKS_URL")

F7T_SSH_CERTIFICATE_WRAPPER = os.environ.get("F7T_SSH_CERTIFICATE_WRAPPER", None)

logging.getLogger().setLevel(logging.INFO)
logging.basicConfig(format='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',datefmt='%Y-%m-%d:%H:%M:%S',level=logging.INFO)


# checks JWT from Keycloak, optionally validates signature. It only receives the content of header's auth pair (not key:content)
def check_header(header):
    if debug:
        logging.info('debug: cscs_api_common: check_header: ' + header)

    # header = "Bearer ey...", remove first 7 chars
    try:
        if realm_pubkey == '':
            if not debug:
                logging.warning("WARNING: cscs_api_common: check_header: REALM_RSA_PUBLIC_KEY is empty, JWT tokens are NOT verified, setup is not set to debug.")
            decoded = jwt.decode(header[7:], verify=False)
        else:
            if AUTH_AUDIENCE == '':
                decoded = jwt.decode(header[7:], realm_pubkey, algorithms=realm_pubkey_type, options={'verify_aud': False})
            else:
                decoded = jwt.decode(header[7:], realm_pubkey, algorithms=realm_pubkey_type, audience=AUTH_AUDIENCE)

        # if AUTH_REQUIRED_SCOPE != '':
        #     if not (AUTH_REQUIRED_SCOPE in decoded['realm_access']['roles']):
        #         return False


        # {"scope": "openid profile firecrest email"}
        if AUTH_REQUIRED_SCOPE != "":
            if AUTH_REQUIRED_SCOPE not in decoded["scope"].split():
                return False

        #if not (decoded['preferred_username'] in ALLOWED_USERS):
        #    return False

        return True

    except jwt.exceptions.InvalidSignatureError:
        logging.error("JWT invalid signature", exc_info=True)
    except jwt.ExpiredSignatureError:
        logging.error("JWT token has expired", exc_info=True)
    except jwt.InvalidAudienceError:
        logging.error("JWT token invalid audience", exc_info=True)
    except jwt.exceptions.InvalidAlgorithmError:
        logging.error("JWT invalid signature algorithm", exc_info=True)
    except Exception:
        logging.error("Bad header or JWT, general exception raised", exc_info=True)

    return False

# returns username
def get_username(header):
    if debug:
        logging.info('debug: cscs_api_common: get_username: ' + header)
    # header = "Bearer ey...", remove first 7 chars
    try:
        if realm_pubkey == '':
            decoded = jwt.decode(header[7:], verify=False)
        else:
#            if AUTH_AUDIENCE == '':
            decoded = jwt.decode(header[7:], realm_pubkey, algorithms=realm_pubkey_type, options={'verify_aud': False})
#            else:
#                decoded = jwt.decode(header[7:], realm_pubkey, algorithms=realm_pubkey_type, audience=AUTH_AUDIENCE)

#        if ALLOWED_USERS != '':
#            if not (decoded['preferred_username'] in ALLOWED_USERS):
#                return None
        # check if it's a service account token
        try:
            if AUTH_ROLE in decoded["realm_access"]["roles"]: # firecrest-sa

                clientId = decoded["clientId"]
                username = decoded["resource_access"][clientId]["roles"][0]
                return username
            return decoded['preferred_username']
        except Exception:
            return decoded['preferred_username']

    except jwt.exceptions.InvalidSignatureError:
        logging.error("JWT invalid signature", exc_info=True)
    except jwt.ExpiredSignatureError:
        logging.error("JWT token has expired", exc_info=True)
    except jwt.InvalidAudienceError:
        logging.error("JWT token invalid audience", exc_info=True)
    except jwt.exceptions.InvalidAlgorithmError:
        logging.error("JWT invalid signature algorithm", exc_info=True)
    except Exception:
        logging.error("Bad header or JWT, general exception raised", exc_info=True)

    return None

# function to check if pattern is in string
def in_str(stringval,words):
    try:
        stringval.index(words)
        return True
    except ValueError:
        return False


# SSH certificates creation
# returns pub key certificate name
def create_certificate(auth_header, cluster, command=None, options=None, exp_time=None):
    """
    Args:
      cluster = system to be executed
      command = command to be executed with the certificate (required)
      option = parameters and options to be executed with {command}
      exp_time = expiration time for SSH certificate
    """

    if debug:
        username = get_username(auth_header)
        logging.info(f"Create certificate for user {username}")

    reqURL = f"{CERTIFICATOR_URL}/?cluster={cluster}"

    if command:
        logging.info(f"\tCommand: {command}")
        reqURL += "&command=" + base64.urlsafe_b64encode(command.encode()).decode()
        if options:
            logging.info(f"\tOptions: {options}")
            reqURL += "&option=" + base64.urlsafe_b64encode(options.encode()).decode()
            if exp_time:
                logging.info(f"\tExpiration: {exp_time} [s]")
                reqURL += f"&exptime={exp_time}"
    else:
        logging.error('Tried to create certificate without command')
        return [None, 1, 'Internal error']

    logging.info(f"Request: {reqURL}")

    try:
        resp = requests.get(reqURL, headers={AUTH_HEADER_NAME: auth_header})

        jcert = resp.json()

        # create temp dir to store certificate for this request
        td = tempfile.mkdtemp(prefix="dummy")

        os.symlink(os.getcwd() + "/user-key.pub", td + "/user-key.pub")  # link on temp dir
        os.symlink(os.getcwd() + "/user-key", td + "/user-key")  # link on temp dir
        certf = open(td + "/user-key-cert.pub", 'w')
        certf.write(jcert["certificate"])
        certf.close()
        # stat.S_IRUSR -> owner has read permission
        os.chmod(td + "/user-key-cert.pub", stat.S_IRUSR)

        # keys: [pub_cert, pub_key, priv_key, temp_dir]
        return [td + "/user-key-cert.pub", td + "/user-key.pub", td + "/user-key", td]
    except URLError as ue:
        logging.error(f"({ue.errno}) -> {ue.strerror}", exc_info=True)
        return [None, ue.errno, ue.strerror]
    except IOError as ioe:
        logging.error(f"({ioe.errno}) -> {ioe.strerror}", exc_info=True)
        return [None, ioe.errno, ioe.strerror]
    except Exception as e:
        logging.error(f"({type(e)}) -> {e}", exc_info=True)
        return [None, -1, e]



# execute remote commands with Paramiko:
def exec_remote_command(auth_header, system, action, file_transfer=None, file_content=None):

    import paramiko, socket

    logging.info('debug: cscs_common_api: exec_remote_command: system: ' + system + '  -  action: ' + action)

    if file_transfer == "storage_cert":
        # storage is using a previously generated cert, save cert list from content
        # cert_list: list of 4 elements that contains
        #   [0] path to the public certificate
        #   [1] path to the public key for user
        #   [2] path to the priv key for user
        #   [3] path to the dir containing 3 previous files
        cert_list = file_content
        username = auth_header
    else:
        # get certificate:
        # if OK returns: [pub_cert, pub_key, priv_key, temp_dir]
        # if FAILED returns: [None, errno, strerror]
        cert_list = create_certificate(auth_header, system, command=action)

        if cert_list[0] == None:
            result = {"error": 1, "msg": "Cannot create certificates"}
            return result

        username = get_username(auth_header)


    [pub_cert, pub_key, priv_key, temp_dir] = cert_list

    # -------------------
    # remote exec with paramiko
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        ipaddr = system.split(':')
        host = ipaddr[0]
        if len(ipaddr) == 1:
            port = 22
        else:
            port = int(ipaddr[1])

        client.connect(hostname=host, port=port,
                       username=username,
                       key_filename=pub_cert,
                       allow_agent=False,
                       look_for_keys=False,
                       timeout=10)

        if F7T_SSH_CERTIFICATE_WRAPPER:
            # read cert to send it as a command to the server
            with open(pub_cert, 'r') as cert_file:
               cert = cert_file.read().rstrip("\n")  # remove newline at the end
            action = cert

        stdin, stdout, stderr = client.exec_command(action)

        if file_transfer == "upload":
            # uploads use "cat", so write to stdin
            stdin.channel.sendall(file_content)
            stdin.channel.shutdown_write()

        output = ""
        error = ""
        finished = 0
        # channels can be ready to exit (exit_status) but there may be still data to read, so
        # force one more read by requiring finished == 2
        while finished < 2:
            if stdout.channel.recv_ready():
                data = stdout.channel.recv(4096)
                while data:
                    output += data.decode()
                    data = stdout.channel.recv(131072)

            if stderr.channel.recv_stderr_ready():
                data = stderr.channel.recv_stderr(4096)
                while data:
                    error += data.decode()
                    data = stderr.channel.recv_stderr(131072)

            if stdout.channel.exit_status_ready() and stderr.channel.exit_status_ready():
                finished += 1

        #exit_status = chan.recv_exit_status()

        stderr_errno = stderr.channel.recv_exit_status()
        stdout_errno = stdout.channel.recv_exit_status()
        # clean "tput: No ..." lines at error output
        stderr_errda = clean_err_output(error)
        stdout_errda = clean_err_output(output)

        if file_transfer == "download":
            outlines = output
        else:
            # replace newlines with $ for parsing
            outlines = output.replace('\n', '$')[:-1]

        logging.info(f"sdterr: ({stderr_errno}) --> {stderr_errda}")
        logging.info(f"stdout: ({stdout_errno}) --> {stdout_errda}")
        logging.info(f"sdtout: ({stdout_errno}) --> {outlines}")

        # TODO: change precedence of error, because in /xfer-external/download this gives error and it s not an error
        if stderr_errno == 0:
            if stderr_errda and not in_str(stderr_errda,"Could not chdir to home directory"):
                result = {"error": 0, "msg": stderr_errda}
            else:
                result = {"error": 0, "msg": outlines}
        elif stderr_errno > 0:
            result = {"error": stderr_errno, "msg": stderr_errda}
        elif len(stderr_errda) > 0:
            result = {"error": 1, "msg": stderr_errda}


    # first if paramiko exception raise
    except paramiko.ssh_exception.NoValidConnectionsError as e:
        logging.error(type(e), exc_info=True)
        if e.errors:
            for k, v in e.errors.items():
                logging.error(f"errorno: {v.errno}")
                logging.error(f"strerr: {v.strerror}")
                result = {"error": v.errno, "msg": v.strerror}

    except socket.gaierror as e:
        logging.error(type(e), exc_info=True)
        logging.error(e.errno)
        logging.error(e.strerror)
        result = {"error": e.errno, "msg": e.strerror}

    except paramiko.ssh_exception.SSHException as e:
        logging.error(type(e), exc_info=True)
        logging.error(e)
        result = {"error": 1, "msg": str(e)}

    # second: time out
    except socket.timeout as e:
        logging.error(type(e), exc_info=True)
        # timeout has not errno
        logging.error(e)
        result = {"error": 1, "msg": e.strerror}

    except Exception as e:
        logging.error(type(e), exc_info=True)
        result = {"error": 1, "msg": str(e)}

    finally:
        client.close()
        os.remove(pub_cert)
        os.remove(pub_key)
        os.remove(priv_key)
        os.rmdir(temp_dir)

    logging.info(f"Result returned: {result['msg']}")
    return result


# clean TERM errors on stderr
# resaon: some servers produces this error becuase don't set a TERM
def clean_err_output(tex):
    lines = ""

    # python3 tex comes as a byte object, needs to be decoded to a str
    #tex = tex.decode('utf-8')

    for t in tex.split('\n'):
        if t != 'tput: No value for $TERM and no -T specified':
            lines += t

    return lines


def parse_io_error(retval, operation, path):
    """
    As command ended with error, create message to return to user
    Args: retval (from exec_remote_command)
          operation, path:
    return:
        jsonify('error message'), error_code (4xx), optional_header
    """
    header = ''
    if retval["error"] == 13:
        # IOError 13: Permission denied
        header = {"X-Permission-Denied": "User does not have permissions to access machine or paths"}
    elif retval["error"] == 2:
        # IOError 2: no such file
        header = {"X-Invalid-Path": f"{path} is invalid."}
    elif retval["error"] == -2:
        # IOError -2: name or service not known
        header = {"X-Machine-Not-Available": "Machine is not available"}
    elif in_str(retval["msg"],"Permission") or in_str(retval["msg"],"OPENSSH"):
        header = {"X-Permission-Denied": "User does not have permissions to access machine or paths"}

    return jsonify(description = f"Failed to {operation}"), 400, header



# function to call create task entry API in Queue FS, returns task_id for new task
def create_task(auth_header,service=None):

    # returns {"task_id":task_id}
    # first try to get up task microservice:
    try:
        # X-Firecrest-Service: service that created the task
        req = requests.post(f"{TASKS_URL}/",
                           headers={AUTH_HEADER_NAME: auth_header, "X-Firecrest-Service":service})

    except requests.exceptions.ConnectionError as e:
        logging.error(type(e), exc_info=True)
        logging.error(e)
        return -1

    if req.status_code != 201:
        return -1

    logging.info(json.loads(req.content))
    resp = json.loads(req.content)
    task_id = resp["hash_id"]

    return task_id


# function to call update task entry API in Queue FS
def update_task(task_id, auth_header, status, msg = None, is_json=False):

    logging.info(f"{TASKS_URL}/{task_id}")

    if is_json:
        data = {"status": status, "msg": msg}
        req = requests.put(f"{TASKS_URL}/{task_id}",
                            json=data, headers={AUTH_HEADER_NAME: auth_header})
    else:
        data = {"status": status, "msg": msg}
        req = requests.put(f"{TASKS_URL}/{task_id}",
                            data=data, headers={AUTH_HEADER_NAME: auth_header})

    resp = json.loads(req.content)

    return resp

# function to call update task entry API in Queue FS
def expire_task(task_id,auth_header):

    logging.info(f"{TASKS_URL}/task-expire/{task_id}")


    req = requests.post(f"{TASKS_URL}/task-expire/{task_id}",
                            headers={AUTH_HEADER_NAME: auth_header})

    resp = json.loads(req.content)

    return resp

# function to check task status:
def get_task_status(task_id,auth_header):

    logging.info(f"{TASKS_URL}/{task_id}")

    try:
        retval = requests.get(f"{TASKS_URL}/{task_id}",
                           headers={AUTH_HEADER_NAME: auth_header})

        if retval.status_code != 200:
            return -1

        data = retval.json()
        logging.info(data["task"]["status"])

        try:
            return data["task"]["status"]
        except KeyError as e:
            logging.error(e)
            return -1

    except requests.exceptions.ConnectionError as e:
        logging.error(type(e), exc_info=True)
        logging.error(e)
        return -1


# wrapper to check if AUTH header is correct
# decorator use:
#
# @app.route("/endpoint", methods=["GET","..."])
# @check_auth_header
# def function_that_check_header():
# .....
def check_auth_header(func):
    @functools.wraps(func)
    def wrapper_check_auth_header(*args, **kwargs):
        try:
            auth_header = request.headers[AUTH_HEADER_NAME]
        except KeyError:
            logging.error("No Auth Header given")
            return jsonify(description="No Auth Header given"), 401
        if not check_header(auth_header):
            return jsonify(description="Invalid header"), 401

        return func(*args, **kwargs)
    return wrapper_check_auth_header
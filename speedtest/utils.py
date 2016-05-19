"""
Common test functions
"""
import os
import urllib
import shutil
import pytest
import time
import subprocess

from contextlib import contextmanager

THISDIR = os.path.dirname(os.path.abspath(__file__))
DISTDIR = os.path.join(os.path.dirname(THISDIR), "dist")
CLCACHE = os.path.join(DISTDIR, "cl.exe")

EMPTY_CACHE = "empty_cache"
RETAIN_CACHE = "retain_cache"


def check_program(envs, program):
    """
    Skip the current test/fixture if program is not in envs[PATH]
    :param envs: look for PATH in this env dict
    :param program: program (eg, perl.exe) to look for
    :return:
    """
    assert envs is not None
    assert "PATH" in envs

    for pathdir in envs["PATH"].split(os.pathsep):
        if os.path.isfile(os.path.join(pathdir, program)):
            return True
    pytest.skip("cannot find {} on PATH".format(program))


def retry_delete(path):
    """
    Repeatedly attempt to delete path
    :param path:
    :return:
    """
    for _ in range(30):
        # antivirus might be busy in here..
        try:
            shutil.rmtree(path)
            return
        except WindowsError:
            time.sleep(1)
    if os.path.exists(path):
        raise Exception("could not delete {}".format(path))


@contextmanager
def block_message(message):
    """
    Emit "begin .. end" messages for a block of code
    :param message:
    """
    started = time.time()
    print "\n..begin {} .. ".format(message)

    try:
        yield
        result = "OK"
    except:
        result = "ERROR"
        raise
    finally:
        print "\n..end {} {}.. ({}sec)".format(message, result, time.time() - started)


def find_visual_studio():
    """
    Attempt to find vs 11, 12 or 13
    :return:
    """
    vcvers = ["13.0", "12.0", "11.0"]
    for vc in vcvers:
        vcdir = os.path.join("c:\\", "Program Files (x86)",
                             "Microsoft Visual Studio {}".format(vc),
                             "VC", "bin")
        vcvars = os.path.join(vcdir, "vcvars32.bat")
        if os.path.exists(vcvars):
            return vcdir, vcvars

    raise Exception("cannot find visual studio!")


def get_vc_envs():
    """
    Get the visual studio dev env vars
    :return:
    """
    if get_vc_envs.envs is None:
        envs = dict(os.environ)
        _, vcvars = find_visual_studio()
        with block_message("getting vc envs"):
            getenvs = subprocess.check_output([vcvars, '>', 'NUL', '&&', 'set'], shell=True)
            for line in getenvs.splitlines():
                if "=" in line:
                    name, val = line.split("=", 1)
                    envs[name.upper()] = val
        get_vc_envs.envs = envs
    return get_vc_envs.envs
get_vc_envs.envs = None


def download_file(url, localfile):
    """
    Download the given url and save it at localfile
    :param url:
    :param localfile:
    :return:
    """
    if not os.path.exists(localfile):
        with block_message("download " + localfile):
            urllib.urlretrieve(url, localfile + ".part")
            os.rename(localfile + ".part", localfile)


def common_module_setup():
    """
    Called by setup_module() in tests
    :return:
    """
    os.chdir(DISTDIR)
    if not os.path.isfile(CLCACHE):
        pytest.fail("please build the exe first")
    find_visual_studio()

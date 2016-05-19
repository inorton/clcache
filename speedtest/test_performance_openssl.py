#!/usr/bin/python
"""
A py.test test that attempts to build openssl and benchmark the effect of clcache
"""
import sys
import os
import pytest
import zipfile
import subprocess

from utils import block_message, retry_delete, common_module_setup, check_program, get_vc_envs, download_file
from utils import DISTDIR, RETAIN_CACHE, EMPTY_CACHE

from fixtures import clcache_envs

OPENSSL_ZIP = "OpenSSL_1_0_2-stable.zip"
OPENSSL_URL = "https://codeload.github.com/openssl/openssl/zip/OpenSSL_1_0_2-stable"
SOURCES = None


def clean_build():
    """
    Unpack the openssl source, possibly deleting the previous one
    :return:
    """
    with zipfile.ZipFile(OPENSSL_ZIP, "r") as unzip:
        folder = unzip.namelist()[0]
        if os.path.exists(folder):
            with block_message("delete old openssl folder"):
                retry_delete(folder)

        with block_message("unzip openssl"):
            unzip.extractall()

        global SOURCES
        SOURCES = folder.rstrip("/")


def configure_openssl(envs):
    """
    Run the configure steps (requires perl)
    :param envs:
    :return:
    """
    check_program(envs, "nmake.exe")
    check_program(envs, "perl.exe")

    with block_message("configure openssl"):
        subprocess.check_call(["perl",
                               "Configure", "VC-WIN32", "no-asm", "--prefix=c:\openssl"],
                              env=envs,
                              cwd=SOURCES)

    with block_message("generate makefiles"):
        subprocess.check_call([os.path.join("ms", "do_ms.bat")],
                              shell=True,
                              env=envs,
                              cwd=SOURCES)


def setup_function(request):
    """
    Ensure a clean build tree before each test
    :return:
    """
    os.chdir(DISTDIR)
    clean_build()
    configure_openssl(get_vc_envs())


def setup_module():
    """
    Check that our exe has been built.
    :return:
    """
    common_module_setup()
    download_file(OPENSSL_URL, OPENSSL_ZIP)


def replace_wipe_cflags(filename):
    """
    Open the nmake file given and turn off PDB generation for .obj files
    :param filename:
    :return:
    """
    lines = []
    with open(filename, "rb") as makefile:
        for line in makefile.readlines():
            if line.startswith("APP_CFLAG="):
                lines.append("APP_CFLAG=")
            elif line.startswith("LIB_CFLAG="):
                lines.append("LIB_CFLAG=/Zl")
            else:
                lines.append(line.rstrip())

    with open(filename, "wb") as makefile:
        for line in lines:
            makefile.write(line + "\r\n")


def build_openssl(addpath=None, envs=get_vc_envs(), pdbs=False):
    """
    Build openssl, optionally prefixing addpath to $PATH
    :param addpath:
    :param envs: env var dict to use
    :param pdbs: if False, turn off pdb generation in the makefile
    :return:
    """
    nmakefile = os.path.join("ms", "nt.mak")
    if not pdbs:
        replace_wipe_cflags(os.path.join(SOURCES, nmakefile))

    if addpath is not None:
        envs["PATH"] = addpath + os.pathsep + envs["PATH"]

    try:
        with block_message("running nmake"):
            subprocess.check_output(["nmake", "-f", nmakefile],
                                    shell=True,
                                    env=envs,
                                    cwd=SOURCES)
    except subprocess.CalledProcessError as cpe:
        print cpe.output
        raise


def test_build_nocache():
    """
    Time an openssl build with no caching involved at all
    :return:
    """
    build_openssl()


@pytest.mark.parametrize("cache_setting", [EMPTY_CACHE, RETAIN_CACHE])
def test_build_withclcache(clcache_envs, cache_setting):
    """
    Time an openssl build with a cold cache
    :param clcache_envs: clcache environment vars fixture
    :param cache_setting: if True, delete the cache from disk
    :return:
    """
    if cache_setting == EMPTY_CACHE:
        retry_delete(clcache_envs["CLCACHE_DIR"])
    build_openssl(DISTDIR, clcache_envs)


if __name__ == "__main__":
    pytest.main(sys.argv[1:])

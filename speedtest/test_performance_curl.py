#!/usr/bin/python
"""
A py.test test that attempts to build libcurl and benchmark the effect of clcache
"""

import sys
import os
import pytest
import urllib
import zipfile
import subprocess

from utils import block_message, retry_delete, common_module_setup, check_program, get_vc_envs, download_file
from utils import DISTDIR, RETAIN_CACHE, EMPTY_CACHE

from fixtures import clcache_envs

CURL_URL = "https://github.com/curl/curl/archive/curl-7_49_0.zip"
CURL_ZIP = "curl-7_49_0.zip"
SOURCES = None


def clean_build():
    """
    Unpack the curl source, possibly deleting the previous one
    :return:
    """
    with zipfile.ZipFile(CURL_ZIP, "r") as unzip:
        folder = unzip.namelist()[0]
        if os.path.exists(folder):
            with block_message("delete old curl folder"):
                retry_delete(folder)

        with block_message("unzip curl"):
            unzip.extractall()

        global SOURCES
        SOURCES = folder.rstrip("/")


def setup_function(request):
    """
    Ensure a clean build tree before each test
    :return:
    """
    os.chdir(DISTDIR)
    clean_build()


def setup_module():
    """
    Check that our exe has been built.
    :return:
    """
    common_module_setup()
    download_file(CURL_URL, CURL_ZIP)


def build_curl(addpath=None, envs=get_vc_envs(), pdbs=False):
    """
    Build curl with nmake
    :return:
    """
    check_program(envs, "nmake.exe")
    workdir = os.path.join(SOURCES, "winbuild")

    if addpath is not None:
        envs["PATH"] = addpath + os.pathsep + envs["PATH"]

    gen_pdbs = "GEN_PDB=no"
    if pdbs:
        gen_pdbs = "GEN_PDB=yes"

    with block_message("build curl"):
        subprocess.check_output(["nmake", "-f", "Makefile.vc", "mode=static",
                                 "ENABLE_SSPI=no",
                                 "ENABLE_IPV6=no",
                                 "ENABLE_IDN=no",
                                 gen_pdbs],
                                shell=True,
                                cwd=workdir,
                                env=envs)


def test_build_nocache():
    """
    Build curl with no caching
    :return:
    """
    build_curl()


@pytest.mark.parametrize("cache_setting", [EMPTY_CACHE, RETAIN_CACHE])
def test_build_withclcache(clcache_envs, cache_setting):
    """
    Time a curl build with a cold cache
    :param clcache_envs: clcache environment vars fixture
    :param cache_setting: if True, delete the cache from disk
    :return:
    """
    if cache_setting == EMPTY_CACHE:
        retry_delete(clcache_envs["CLCACHE_DIR"])
    build_curl(DISTDIR, clcache_envs)


if __name__ == "__main__":
    pytest.main(sys.argv[1:])

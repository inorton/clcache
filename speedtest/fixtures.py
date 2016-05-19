"""
Test fixtures
"""

import os
import pytest
from utils import find_visual_studio, get_vc_envs


@pytest.fixture()
def clcache_envs():
    """
    return a dict of envs suitable for clcache to work with
    :return:
    """
    vcdir, _ = find_visual_studio()
    envs = get_vc_envs()
    cachedir = os.path.join("clcache_cachedir")
    envs["CLCACHE_DIR"] = cachedir
    envs["CLCACHE_CL"] = os.path.join(vcdir, "cl.exe")
    return envs

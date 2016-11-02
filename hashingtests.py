#!/usr/bin/env python3
#
# This file is part of the clcache project.
#
# The contents of this file are subject to the BSD 3-Clause License, the
# full text of which is available in the accompanying LICENSE file at the
# root directory of this project.
#

import os
import unittest
import shutil
import clcache
import uuid

THIS_DIR = os.path.dirname(__file__)
ASSETS_DIR = os.path.join(THIS_DIR, "tests", "hashtests")


class TestFileHashing(unittest.TestCase):
    """
    Test class to create lots of test files and test performance in hashing them.
    """
    NUM_TEST_FILES = 1000
    TEST_FILES = list()

    @classmethod
    def setUpClass(cls):
        """
        Create many files in folders so that we can hash them in the test
        :return:
        """
        if os.path.isdir(ASSETS_DIR):
            shutil.rmtree(ASSETS_DIR)
        os.makedirs(ASSETS_DIR)

        for _ in range(cls.NUM_TEST_FILES):
            fstr = str(uuid.uuid4())
            fdir = fstr[:3]
            fname = fstr[3:]
            if not os.path.isdir(os.path.join(ASSETS_DIR, fdir)):
                os.makedirs(os.path.join(ASSETS_DIR, fdir))
            filepath = os.path.join(ASSETS_DIR, fdir, fname)
            with open(filepath, "w") as fhandle:
                fhandle.write("{}".format(fname * 8000))
            cls.TEST_FILES.append(filepath)

    def testExistsManyFiles(self):
        """
        Simply check all the files exist
        :return:
        """
        for filepath in self.TEST_FILES:
            assert os.path.exists(filepath)

    def testHashManyFiles(self):
        """
        Hash all our files
        """
        for filepath in self.TEST_FILES:
            filehash = clcache.getFileHash(filepath)
            assert filehash


if __name__ == "__main__":
    unittest.TestCase.longMessage = True
    unittest.main()

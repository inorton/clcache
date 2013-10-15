#!/usr/bin/env python
#
# clcache.py - a compiler cache for Microsoft Visual Studio
#
# Copyright (c) 2010, 2011, 2012, 2013 froglogic GmbH <raabe@froglogic.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the <organization> nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
from ctypes import windll, wintypes
import codecs
from collections import defaultdict
import hashlib
import json
import os
from shutil import copyfile, rmtree
import subprocess
from subprocess import Popen, PIPE, STDOUT
import sys
import multiprocessing
import re
import shlex

class ObjectCacheLockException(Exception):
    pass

class ObjectCacheLock:
    """ Implements a lock for the object cache which
    can be used in 'with' statements. """
    INFINITE = 0xFFFFFFFF

    def __init__(self, mutexName, timeoutMs):
        mutexName = 'Local\\' + mutexName
        self._mutex = windll.kernel32.CreateMutexW(
            wintypes.c_int(0),
            wintypes.c_int(0),
            unicode(mutexName))
        self._timeoutMs = timeoutMs
        self._acquired = False
        assert self._mutex

    def __enter__(self):
        if not self._acquired:
            self.acquire()

    def __exit__(self, type, value, traceback):
        if self._acquired:
            self.release()

    def __del__(self):
        windll.kernel32.CloseHandle(self._mutex)

    def acquire(self):
        WAIT_ABANDONED = 0x00000080
        result = windll.kernel32.WaitForSingleObject(
            self._mutex, wintypes.c_int(self._timeoutMs))
        if result != 0 and result != WAIT_ABANDONED:
            errorString ='Error! WaitForSingleObject returns {result}, last error {error}'.format(
                result=result,
                error=windll.kernel32.GetLastError())
            raise ObjectCacheLockException(errorString)
        self._acquired = True

    def release(self):
        windll.kernel32.ReleaseMutex(self._mutex)
        self._acquired = False

class ObjectCache:
    def __init__(self):
        try:
            self.dir = os.environ["CLCACHE_DIR"]
        except KeyError:
            self.dir = os.path.join(os.path.expanduser("~"), "clcache")
        if not os.path.exists(self.dir):
            os.makedirs(self.dir)
        lockName = self.cacheDirectory().replace(':', '-').replace('\\', '-')
        self.lock = ObjectCacheLock(lockName, 10 * 1000)
        self.directmode = "CLCACHE_DIRECT" in os.environ

    def cacheDirectory(self):
        return self.dir

    def clean(self, stats, maximumSize):
        with self.lock:
            currentSize = stats.currentCacheSize()
            if currentSize < maximumSize:
                return

            # Free at least 10% to avoid cleaning up too often which
            # is a big performance hit with large caches.
            effectiveMaximumSize = maximumSize * 0.9

            objects = [os.path.join(root, "object")
                       for root, folder, files in os.walk(self.dir)
                       if "object" in files]

            objectInfos = [(os.stat(fn), fn) for fn in objects]
            objectInfos.sort(key=lambda t: t[0].st_atime)

            for stat, fn in objectInfos:
                rmtree(os.path.split(fn)[0])
                currentSize -= stat.st_size
                if currentSize < effectiveMaximumSize:
                    break

            stats.setCacheSize(currentSize)

    def getDirectIncludeFiles(self, compilerBinary, commandLine):
        files=[]

        ppcmd = [compilerBinary, "/E"]
        ppcmd += [arg for arg in commandLine if not arg in ("-c", "/c")]
        preprocessor = Popen(ppcmd, stdout=PIPE, stderr=PIPE)
        (preprocessedSourceCode, pperr) = preprocessor.communicate()

        re_ldirective = re.compile("^#line\s+[\d]+\s+\"([^\"]+)\"")

        for l in preprocessedSourceCode.split('\n'):
            x = re_ldirective.match(l)
            if x:
                files.append(os.path.abspath(x.group(1)))

        for f in set(files):
            files.append( ( self.hashFile(f), f ) )

        return files

    def hashFile(self, filepath):
        h = hashlib.md5()
        f = open(filepath,"r")
        for data in f:
            h.update(data)
        f.close()
        return h.hexdigest()

    def computeKey(self, compilerBinary, commandLine, srcFile):

        if self.directmode:
            return self.computeKeyDirect( compilerBinary, commandLine, srcFile )

        return self.computeKeyEP( compilerBinary, commandLine )

    def computeKeyDirect(self, compilerBinary, commandLine, srcFile):
        normalizedCmdLine = self._normalizedCommandLine(commandLine)

        stat = os.stat(compilerBinary)
        h = hashlib.md5()
        h.update(str(stat.st_mtime))
        h.update(str(stat.st_size))
        h.update(' '.join(normalizedCmdLine))
        envhash = h.hexdigest()

        srchash = self.hashFile( srcFile ) 
        return envhash + "-" + srchash

    def hasHit(self, compilerBinary, commandLine, hashkey):
        if self.directmode:
          return self.checkManifest( compilerBinary, commandLine, hashkey)

        else:
          return hasEntry(hashkey) 

    def computeKeyEP(self, compilerBinary, commandLine):
        ppcmd = [compilerBinary, "/EP"]
        ppcmd += [arg for arg in commandLine if not arg in ("-c", "/c")]
        preprocessor = Popen(ppcmd, stdout=PIPE, stderr=PIPE)
        (preprocessedSourceCode, pperr) = preprocessor.communicate()

        if preprocessor.returncode != 0:
            sys.stderr.write(pperr)
            sys.stderr.write("clcache: preprocessor failed\n")
            sys.exit(preprocessor.returncode)

        normalizedCmdLine = self._normalizedCommandLine(commandLine)

        stat = os.stat(compilerBinary)
        h = hashlib.md5()
        h.update(str(stat.st_mtime))
        h.update(str(stat.st_size))
        h.update(' '.join(normalizedCmdLine))
        h.update(preprocessedSourceCode)
        return h.hexdigest()

    def checkManifest(self, compiler, argv, hashkey):
        with self.lock:
            if self.hasManifest(hashkey):
              md = self.getManifest( hashkey )
              for fn in md:
                  if not os.path.exists(fn):
                      return False
                  check = self.hashFile(fn)
                  if check != md[fn]:
                      return False
              return True

        return False 

    def hasManifest(self, key):
        with self.lock:
            return os.path.exists(self.cachedManifestName(key))

    def hasEntry(self, key):
        # in direct mode, the key is the hash of the command line args and
        # source file

        # in old clcache mode, the key is the hash of the pre-processed sources
        with self.lock:
            return os.path.exists(self.cachedObjectName(key))

    def setEntry(self, key, objectFileName, compilerOutput, compilerError):
        with self.lock:
            if not os.path.exists(self._cacheEntryDir(key)):
                os.makedirs(self._cacheEntryDir(key))
            copyfile(objectFileName, self.cachedObjectName(key))
            open(self._cachedCompilerOutputName(key), 'w').write(compilerOutput)
            open(self._cachedCompilerErrorName(key), 'w').write(compilerError)


    def cachedObjectName(self, key):
        return os.path.join(self._cacheEntryDir(key), "object")

    def cachedManifestName(self, key):
        return self._cachedCompilerManifestName(key)


    # manifest is a dict of files->hashes of all input source files before
    # preprocessing
    def getManifest(self, key):
        items = dict()
        for line in open(self.cachedManifestName(key), 'r'):
            tmp = line.strip().split(" ",1)
            items[tmp[1]] = tmp[0]
        return items

    def writeManifest(self, key, data=[]):
        with self.lock:
            m = open(self.cachedManifestName(key), 'w')
            for d in data:
                m.write( d[0] + " " + d[1] )
            m.close()

    def cachedCompilerOutput(self, key):
        return open(self._cachedCompilerOutputName(key), 'r').read()

    def cachedCompilerError(self, key):
        return open(self._cachedCompilerErrorName(key), 'r').read()

    def _cacheEntryDir(self, key):
        return os.path.join(self.dir, key[:2], key)

    def _cachedCompilerManifestName(self, key):
        return os.path.join(self._cacheEntryDir(key), "manifest.txt")

    def _cachedCompilerOutputName(self, key):
        return os.path.join(self._cacheEntryDir(key), "output.txt")

    def _cachedCompilerErrorName(self, key):
        return os.path.join(self._cacheEntryDir(key), "error.txt")

    def _normalizedCommandLine(self, cmdline):
        # Remove all arguments from the command line which only influence the
        # preprocessor; the preprocessor's output is already included into the
        # hash sum so we don't have to care about these switches in the
        # command line as well.
        _argsToStrip = ("AI", "C", "E", "P", "FI", "u", "X",
                        "FU", "D", "EP", "Fx", "U", "I")

        # Also remove the switch for specifying the output file name; we don't
        # want two invocations which are identical except for the output file
        # name to be treated differently.
        _argsToStrip += ("Fo",)

        return [arg for arg in cmdline
                if not (arg[0] in "/-" and arg[1:].startswith(_argsToStrip))]

class PersistentJSONDict:
    def __init__(self, fileName):
        self._dirty = False
        self._dict = {}
        self._fileName = fileName
        try:
            self._dict = json.load(open(self._fileName, 'r'))
        except:
            pass

    def save(self):
        if self._dirty:
            json.dump(self._dict, open(self._fileName, 'w'))

    def __setitem__(self, key, value):
        self._dict[key] = value
        self._dirty = True

    def __getitem__(self, key):
        return self._dict[key]

    def __contains__(self, key):
        return key in self._dict


class Configuration:
    _defaultValues = { "MaximumCacheSize": 1024 * 1024 * 1000 }

    def __init__(self, objectCache):
        self._objectCache = objectCache
        with objectCache.lock:
            self._cfg = PersistentJSONDict(os.path.join(objectCache.cacheDirectory(),
                                                        "config.txt"))
        for setting, defaultValue in self._defaultValues.iteritems():
            if not setting in self._cfg:
                self._cfg[setting] = defaultValue

    def maximumCacheSize(self):
        return self._cfg["MaximumCacheSize"]

    def setMaximumCacheSize(self, size):
        self._cfg["MaximumCacheSize"] = size

    def save(self):
        with self._objectCache.lock:
            self._cfg.save()


class CacheStatistics:
    def __init__(self, objectCache):
        self.objectCache = objectCache
        with objectCache.lock:
            self._stats = PersistentJSONDict(os.path.join(objectCache.cacheDirectory(),
                                                          "stats.txt"))
        for k in ["CallsWithoutSourceFile",
                  "CallsWithMultipleSourceFiles",
                  "CallsWithPch",
                  "CallsForLinking",
                  "CacheEntries", "CacheSize",
                  "CacheHits", "CacheMisses"]:
            if not k in self._stats:
                self._stats[k] = 0

    def numCallsWithoutSourceFile(self):
        return self._stats["CallsWithoutSourceFile"]

    def registerCallWithoutSourceFile(self):
        self._stats["CallsWithoutSourceFile"] += 1

    def numCallsWithMultipleSourceFiles(self):
        return self._stats["CallsWithMultipleSourceFiles"]

    def registerCallWithMultipleSourceFiles(self):
        self._stats["CallsWithMultipleSourceFiles"] += 1

    def numCallsWithPch(self):
        return self._stats["CallsWithPch"]

    def registerCallWithPch(self):
        self._stats["CallsWithPch"] += 1

    def numCallsForLinking(self):
        return self._stats["CallsForLinking"]

    def registerCallForLinking(self):
        self._stats["CallsForLinking"] += 1

    def numCacheEntries(self):
        return self._stats["CacheEntries"]

    def registerCacheEntry(self, size):
        self._stats["CacheEntries"] += 1
        self._stats["CacheSize"] += size

    def currentCacheSize(self):
        return self._stats["CacheSize"]

    def setCacheSize(self, size):
        self._stats["CacheSize"] = size

    def numCacheHits(self):
        return self._stats["CacheHits"]

    def registerCacheHit(self):
        self._stats["CacheHits"] += 1

    def numCacheMisses(self):
        return self._stats["CacheMisses"]

    def registerCacheMiss(self):
        self._stats["CacheMisses"] += 1

    def resetCounters(self):
        for k in ["CallsWithoutSourceFile",
                  "CallsWithMultipleSourceFiles",
                  "CallsWithPch",
                  "CallsForLinking",
                  "CacheHits", "CacheMisses"]:
            self._stats[k] = 0

    def save(self):
        with self.objectCache.lock:
            self._stats.save()

class AnalysisResult:
    Ok, NoSourceFile, MultipleSourceFilesSimple, \
        MultipleSourceFilesComplex, CalledForLink, \
        CalledWithPch, ExternalDebugInfo = range(7)

def copyOrLink(srcFilePath, dstFilePath):
    if "CLCACHE_HARDLINK" in os.environ:
        ret = windll.kernel32.CreateHardLinkW(unicode(dstFilePath), unicode(srcFilePath), None)
        if ret != 0:
            # Touch the time stamp of the new link so that the build system
            # doesn't confused by a potentially old time on the file. The
            # hard link gets the same timestamp as the cached file.
            # Note that touching the time stamp of the link also touches
            # the time stamp on the cache (and hence on all over hard
            # links). This shouldn't be a problem though.
            os.utime(dstFilePath, None)
            return

    # If hardlinking fails for some reason (or it's not enabled), just
    # fall back to moving bytes around...
    copyfile(srcFilePath, dstFilePath)

def findCompilerBinary():
    if "CLCACHE_CL" in os.environ:
        path = os.environ["CLCACHE_CL"]
        return path if os.path.exists(path) else None

    frozenByPy2Exe = hasattr(sys, "frozen")
    if frozenByPy2Exe:
        myExecutablePath = unicode(sys.executable, sys.getfilesystemencoding())

    for dir in os.environ["PATH"].split(os.pathsep):
        path = os.path.join(dir, "cl.exe")
        if os.path.exists(path):
            if not frozenByPy2Exe:
                return path

            # Guard against recursively calling ourselves
            if path != myExecutablePath:
                return path
    return None


def printTraceStatement(msg):
    if "CLCACHE_LOG" in os.environ:
        script_dir = os.path.realpath(os.path.dirname(sys.argv[0]))
        print os.path.join(script_dir, "clcache.py") + " " + msg

def expandCommandLine(cmdline):
    ret = []

    for arg in cmdline:
        if arg[0] == '@':
            includeFile = arg[1:]
            with open(includeFile, 'rb') as file:
                rawBytes = file.read()

            encoding = None

            encodingByBOM = {
                codecs.BOM_UTF32_BE: 'utf-32-be',
                codecs.BOM_UTF32_LE: 'utf-32-le',
                codecs.BOM_UTF16_BE: 'utf-16-be',
                codecs.BOM_UTF16_LE: 'utf-16-le',
            }

            for bom, enc in encodingByBOM.items():
                if rawBytes.startswith(bom):
                    encoding = encodingByBOM[bom]
                    rawBytes = rawBytes[len(bom):]
                    break

            includeFileContents = rawBytes.decode(encoding) if encoding is not None else rawBytes

            ret.extend(expandCommandLine(shlex.split(includeFileContents, posix=False)))
        else:
            ret.append(arg)

    return ret

def parseCommandLine(cmdline):
    optionsWithParameter = ['Ob', 'Gs', 'Fa', 'Fd', 'Fm',
                            'Fp', 'FR', 'doc', 'FA', 'Fe',
                            'Fo', 'Fr', 'AI', 'FI', 'FU',
                            'D', 'U', 'I', 'Zp', 'vm',
                            'MP', 'Tc', 'V', 'wd', 'wo',
                            'W', 'Yc', 'Yl', 'Tp', 'we',
                            'Yu', 'Zm', 'F']
    options = defaultdict(list)
    responseFile = ""
    sourceFiles = []
    i = 0
    while i < len(cmdline):
        arg = cmdline[i]

        # Plain arguments startign with / or -
        if arg[0] == '/' or arg[0] == '-':
            isParametrized = False
            for opt in optionsWithParameter:
                if arg[1:len(opt)+1] == opt:
                    isParametrized = True
                    key = opt
                    if len(arg) > len(opt) + 1:
                        value = arg[len(opt)+1:]
                    else:
                        value = cmdline[i+1]
                        i += 1
                    options[key].append(value)
                    break

            if not isParametrized:
                options[arg[1:]] = []

        # Reponse file
        elif arg[0] == '@':
            responseFile = arg[1:]

        # Source file arguments
        else:
            sourceFiles.append(arg)

        i += 1

    return options, responseFile, sourceFiles

def analyzeCommandLine(cmdline):
    options, responseFile, sourceFiles = parseCommandLine(cmdline)
    compl = False

    # Technically, it would be possible to support /Zi: we'd just need to
    # copy the generated .pdb files into/out of the cache.
    if 'Zi' in options:
        return AnalysisResult.ExternalDebugInfo, None, None
    if 'Yu' in options:
        return AnalysisResult.CalledWithPch, None, None
    if 'Tp' in options:
        sourceFiles += options['Tp']
        compl = True
    if 'Tc' in options:
        sourceFiles += options['Tc']
        compl = True

    if 'link' in options or not 'c' in options:
        return AnalysisResult.CalledForLink, None, None

    if len(sourceFiles) == 0:
        return AnalysisResult.NoSourceFile, None, None

    if len(sourceFiles) > 1:
        if compl:
            return AnalysisResult.MultipleSourceFilesComplex, None, None
        return AnalysisResult.MultipleSourceFilesSimple, sourceFiles, None

    outputFile = None
    if 'Fo' in options:
        outputFile = options['Fo'][0]

        if os.path.isdir(outputFile):
            srcFileName = os.path.basename(sourceFiles[0])
            outputFile = os.path.join(outputFile,
                                      os.path.splitext(srcFileName)[0] + ".obj")
    else:
        srcFileName = os.path.basename(sourceFiles[0])
        outputFile = os.path.join(os.getcwd(),
                                  os.path.splitext(srcFileName)[0] + ".obj")

    # Strip quotes around file names; seems to happen with source files
    # with spaces in their names specified via a response file generated
    # by Visual Studio.
    if outputFile.startswith('"') and outputFile.endswith('"'):
        outputFile = outputFile[1:-1]

    printTraceStatement("Compiler output file: '%s'" % outputFile)
    return AnalysisResult.Ok, sourceFiles[0], outputFile


def invokeRealCompiler(compilerBinary, cmdLine, captureOutput=False):
    realCmdline = [compilerBinary] + cmdLine
    printTraceStatement("Invoking real compiler as '%s'" % ' '.join(realCmdline))

    returnCode = None
    output = None
    stderr = None
    if captureOutput:
        compilerProcess = Popen(realCmdline, universal_newlines=True, stdout=PIPE, stderr=PIPE)
        output, stderr = compilerProcess.communicate()
        returnCode = compilerProcess.returncode
    else:
        returnCode = subprocess.call(realCmdline, universal_newlines=True)

    printTraceStatement("Real compiler returned code %d" % returnCode)
    return returnCode, output, stderr

# Given a list of Popen objects, removes and returns
# a completed Popen object.
#
# FIXME: this is a bit inefficient, Python on Windows does not appear
# to provide any blocking "wait for any process to complete" out of the
# box.
def waitForAnyProcess(procs):
    out = [p for p in procs if p.poll() != None]
    if len(out) >= 1:
        out = out[0]
        procs.remove(out)
        return out

    # Damn, none finished yet.
    # Do a blocking wait for the first one.
    # This could waste time waiting for one process while others have
    # already finished :(
    out = procs.pop(0)
    out.wait()
    return out

# Returns the amount of jobs which should be run in parallel when
# invoked in batch mode.
#
# The '/MP' option determines this, which may be set in cmdLine or
# in the CL environment variable.
def jobCount(cmdLine):
    switches = []

    if 'CL' in os.environ:
        switches.extend(os.environ['CL'].split(' '))

    switches.extend(cmdLine)

    mp_switch = [switch for switch in switches if re.search(r'^/MP\d+$', switch) != None]
    if len(mp_switch) == 0:
        return 1

    # the last instance of /MP takes precedence
    mp_switch = mp_switch.pop()

    count = mp_switch[3:]
    if count != "":
        return int(count)

    # /MP, but no count specified; use CPU count
    try:
        return multiprocessing.cpu_count()
    except:
        # not expected to happen
        return 2

# Run commands, up to j concurrently.
# Aborts on first failure and returns the first non-zero exit code.
def runJobs(commands, j=1):
    running = []
    returncode = 0

    while len(commands):

        while len(running) > j:
            thiscode = waitForAnyProcess(running).returncode
            if thiscode != 0:
                return thiscode

        thiscmd = commands.pop(0)
        running.append(Popen(thiscmd))

    while len(running) > 0:
        thiscode = waitForAnyProcess(running).returncode
        if thiscode != 0:
            return thiscode

    return 0


# re-invoke clcache.py once per source file.
# Used when called via nmake 'batch mode'.
# Returns the first non-zero exit code encountered, or 0 if all jobs succeed.
def reinvokePerSourceFile(cmdLine, sourceFiles):

    printTraceStatement("Will reinvoke self for: [%s]" % '] ['.join(sourceFiles))
    commands = []
    for sourceFile in sourceFiles:
        # The child command consists of clcache.py ...
        newCmdLine = [sys.executable, sys.argv[0]]

        for arg in cmdLine:
            # and the current source file ...
            if arg == sourceFile:
                newCmdLine.append(arg)
            # and all other arguments which are not a source file
            elif not arg in sourceFiles:
                newCmdLine.append(arg)

        printTraceStatement("Child: [%s]" % '] ['.join(newCmdLine))
        commands.append(newCmdLine)

    return runJobs(commands, jobCount(cmdLine))

def printStatistics():
    cache = ObjectCache()
    with cache.lock:
        stats = CacheStatistics(cache)
        cfg = Configuration(cache)
        print """clcache statistics:
  current cache dir        : %s
  cache size               : %d bytes
  maximum cache size       : %d bytes
  cache entries            : %d
  cache hits               : %d
  cache misses             : %d
  called for linking       : %d
  called w/o sources       : %d
  calls w/ multiple sources: %d
  calls w/ PCH:              %d""" % (
           cache.cacheDirectory(),
           stats.currentCacheSize(),
           cfg.maximumCacheSize(),
           stats.numCacheEntries(),
           stats.numCacheHits(),
           stats.numCacheMisses(),
           stats.numCallsForLinking(),
           stats.numCallsWithoutSourceFile(),
           stats.numCallsWithMultipleSourceFiles(),
           stats.numCallsWithPch())

def resetStatistics():
    cache = ObjectCache()
    with cache.lock:
        stats = CacheStatistics(cache)
        stats.resetCounters()
        stats.save()
    print 'Statistics reset'

if len(sys.argv) == 2 and sys.argv[1] == "--help":
    print """\
clcache.py v2.0.0
  --help   : show this help
  -s       : print cache statistics
  -z       : reset cache statistics
  -M <size>: set maximum cache size (in bytes)
"""
    sys.exit(0)

if len(sys.argv) == 2 and sys.argv[1] == "-s":
    printStatistics()
    sys.exit(0)

if len(sys.argv) == 2 and sys.argv[1] == "-z":
    resetStatistics()
    sys.exit(0)

if len(sys.argv) == 3 and sys.argv[1] == "-M":
    cache = ObjectCache()
    with cache.lock:
        cfg = Configuration(cache)
        cfg.setMaximumCacheSize(int(sys.argv[2]))
        cfg.save()
    sys.exit(0)

compiler = findCompilerBinary()
if not compiler:
    print "Failed to locate cl.exe on PATH (and CLCACHE_CL is not set), aborting."
    sys.exit(1)

printTraceStatement("Found real compiler binary at '%s'" % compiler)

if "CLCACHE_DISABLE" in os.environ:
    sys.exit(invokeRealCompiler(compiler, sys.argv[1:])[0])

printTraceStatement("Parsing given commandline '%s'" % sys.argv[1:] )

cmdLine = expandCommandLine(sys.argv[1:])
printTraceStatement("Expanded commandline '%s'" % cmdLine )
analysisResult, sourceFile, outputFile = analyzeCommandLine(cmdLine)

if analysisResult == AnalysisResult.MultipleSourceFilesSimple:
    sys.exit(reinvokePerSourceFile(cmdLine, sourceFile))

cache = ObjectCache()
stats = CacheStatistics(cache)
if analysisResult != AnalysisResult.Ok:
    with cache.lock:
        if analysisResult == AnalysisResult.NoSourceFile:
            printTraceStatement("Cannot cache invocation as %s: no source file found" % (' '.join(cmdLine)) )
            stats.registerCallWithoutSourceFile()
        elif analysisResult == AnalysisResult.MultipleSourceFilesComplex:
            printTraceStatement("Cannot cache invocation as %s: multiple source files found" % (' '.join(cmdLine)) )
            stats.registerCallWithMultipleSourceFiles()
        elif analysisResult == AnalysisResult.CalledWithPch:
            printTraceStatement("Cannot cache invocation as %s: precompiled headers in use" % (' '.join(cmdLine)) )
            stats.registerCallWithPch()
        elif analysisResult == AnalysisResult.CalledForLink:
            printTraceStatement("Cannot cache invocation as %s: called for linking" % (' '.join(cmdLine)) )
            stats.registerCallForLinking()
        elif analysisResult == AnalysisResult.ExternalDebugInfo:
            printTraceStatement("Cannot cache invocation as %s: external debug information (/Zi) is not supported" % (' '.join(cmdLine)) )
        stats.save()
    sys.exit(invokeRealCompiler(compiler, sys.argv[1:])[0])

cachekey = cache.computeKey(compiler, cmdLine, sourceFile)
if cache.hasHit( compiler, cmdLine, cachekey ):
    with cache.lock:
        stats.registerCacheHit()
        stats.save()
    printTraceStatement("Reusing cached object for key " + cachekey + " for " +
                        "output file " + outputFile)
    if os.path.exists(outputFile):
        os.remove(outputFile)
    copyOrLink(cache.cachedObjectName(cachekey), outputFile)
    sys.stderr.write(cache.cachedCompilerError(cachekey))
    sys.stdout.write(cache.cachedCompilerOutput(cachekey))
    printTraceStatement("Finished. Exit code 0")
    sys.exit(0)
else:
    returnCode, compilerOutput, compilerError = invokeRealCompiler(compiler, cmdLine, captureOutput=True)
    with cache.lock:
        stats.registerCacheMiss()
        if returnCode == 0 and os.path.exists(outputFile):
            if cache.directmode:
                includefiles = cache.getDirectIncludeFiles( compiler, cmdLine )
                m = []
                for fn in includefiles:
                    h = cache.hashFile(fn)
                    m.append((h,fn))
                cache.writeManifest( cachekey, m ) 

            printTraceStatement("Adding file " + outputFile + " to cache using " +
                                "key " + cachekey)
            cache.setEntry(cachekey, outputFile, compilerOutput, compilerError)
            stats.registerCacheEntry(os.path.getsize(outputFile))
            cfg = Configuration(cache)
            cache.clean(stats, cfg.maximumCacheSize())
        stats.save()
    sys.stdout.write(compilerError)
    sys.stdout.write(compilerOutput)
    printTraceStatement("Finished. Exit code %d" % returnCode)
    sys.exit(returnCode)

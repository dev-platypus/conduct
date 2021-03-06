# *****************************************************************************
# conduct - CONvenient Construction Tool
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#
# Module authors:
#   Alexander Lenz <alexander.lenz@frm2.tum.de>
#
# *****************************************************************************

import os
import logging
import platform
import select
import fcntl
import time


from collections import OrderedDict
from ConfigParser import SafeConfigParser
from os import path
from subprocess import Popen, PIPE, CalledProcessError

import conduct
from conduct.param import Parameter, OrderedAttrDict

## Utils classes

class AttrStringifier(object):
    def __getattr__(self, name):
        return name

class OrderedAttrDict(OrderedDict):
    def __init__(self, *args, **kwargs):
        OrderedDict.__init__(self, *args, **kwargs)
        self._init = True

    def __setattr__(self, name, value):
        if not hasattr(self, '_init'):
            return OrderedDict.__setattr__(self, name, value)
        return OrderedDict.__setitem__(self, name, value)

    def __getattr__(self, name):
        if not hasattr(self, '_init'):
            return OrderedDict.__getattr__(self, name)
        return OrderedDict.__getitem__(self, name)

## Util funcs

def analyzeSystem():
    conduct.app.log.info('Analyze current system ...')

    # basic information
    info = platform.uname()
    infoKeys = ('os',
                'hostname',
                'release',
                'version',
                'arch',
                'processor')

    info = OrderedDict(zip(infoKeys, info))

    # detailed arch info
    info.update(zip(('bits', 'binformat'), platform.architecture()))


    for key, value in info.items():
        conduct.app.log.debug('{:<10}: {}'.format(key, value))

    return info

def importFromPath(import_name, prefixes=(), log=None):
    """Imports an object based on a string.

    The should be formatted like: imp.path.to.mod.objname
    """
    if log is None:
        log = conduct.app.log

    if '.' in import_name:
        modname, obj = import_name.rsplit('.', 1)
    else:
        modname, obj = import_name, None
    mod = None
    fromlist = [obj] if obj else []
    errors = []
    for fullname in [modname] + [p + modname for p in prefixes]:
        try:
            mod = __import__(fullname, {}, {}, fromlist)
        except ImportError as err:
            errors.append('[%s] %s' % (fullname, err))
        else:
            break
    if mod is None:
        raise ImportError('Could not import %r: %s' %
                          (import_name, ', '.join(errors)))
    if not obj:
        return mod
    else:
        try:
            return getattr(mod, obj)
        except AttributeError as e:
            raise ImportError('Could not import %s.%s: %s' % (mod.__name__, obj, e))


def getDefaultConfigPath():
    inplacePath = path.join(path.dirname(__file__),
                                '..',
                                '..',
                                'etc',
                                'conduct.conf')
    if path.isfile(inplacePath):
        return inplacePath
    return '/etc/conduct.conf'


def logMultipleLines(strOrList, logFunc=None):
    if logFunc is None:
        logFunc = conduct.app.log.info

    if isinstance(strOrList, str):
        strOrList = strOrList.splitlines()

    for line in strOrList:
        logFunc(line)

def mount(dev, mountpoint, flags='', log=None):
        ensureDirectory(mountpoint)
        systemCall('mount %s %s %s' % (flags, dev, mountpoint),
                   log=log)

def umount(mountpoint, flags='', log=None):
    systemCall('umount %s %s' % (flags, mountpoint),
               log=log)

def systemCall(cmd, sh=True, log=None):
    if log is None:
        log = conduct.app.log

    log.debug('System call [sh:%s]: %s' \
              % (sh, cmd))

    out = []
    proc = None
    poller = None
    outBuf = ['']
    errBuf = ['']

    def pollOutput():
        '''
        Read, log and store output (if any) from processes pipes.
        '''
        removeChars = '\r\n'

         # collect fds with new output
        fds = [entry[0] for entry in poller.poll()]

        if proc.stdout.fileno() in fds:
            while True:
                try:
                    tmp = proc.stdout.read(100)
                except IOError:
                    break
                outBuf[0] += tmp

                while '\n' in outBuf[0]:
                    line, _, outBuf[0] = outBuf[0].partition('\n')
                    log.debug(line)
                    out.append(line + '\n')

                if not tmp:
                    break
        if proc.stderr.fileno() in fds:
            while True:
                try:
                    tmp = proc.stderr.read(100)
                except IOError:
                    break
                errBuf[0] += tmp

                while '\n' in errBuf[0]:
                    line, _, errBuf[0] = errBuf[0].partition('\n')
                    log.warning(line)

                if not tmp:
                    break


    while True:
        if proc is None:
            # create and start process
            proc = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=sh)

            # create poll select
            poller = select.poll()

            flags = fcntl.fcntl(proc.stdout, fcntl.F_GETFL)
            fcntl.fcntl(proc.stdout, fcntl.F_SETFL, flags| os.O_NONBLOCK)

            flags = fcntl.fcntl(proc.stderr, fcntl.F_GETFL)
            fcntl.fcntl(proc.stderr, fcntl.F_SETFL, flags| os.O_NONBLOCK)

            # register pipes to polling
            poller.register(proc.stdout, select.POLLIN)
            poller.register(proc.stderr, select.POLLIN)

        pollOutput()

        if proc.poll() is not None: # proc finished
            break

    # poll once after the process ended to collect all the missing output
    pollOutput()

    # check return code
    if proc.returncode != 0:
        raise RuntimeError(
            CalledProcessError(proc.returncode, cmd, ''.join(out))
            )

    return ''.join(out)

def chrootedSystemCall(chrootDir, cmd, sh=True, mountPseudoFs=True, log=None):
    if log is None:
        log = conduct.app.log

    # determine mount points for pseudo fs
    proc = path.join(chrootDir, 'proc')
    sys = path.join(chrootDir, 'sys')
    dev = path.join(chrootDir, 'dev')
    devpts = path.join(chrootDir, 'dev', 'pts')

    # mount pseudo fs
    if mountPseudoFs:
        mount('proc', proc, '-t proc')
        mount('/sys', sys, '--rbind')
        mount('/dev', dev, '--rbind')

    try:
        # exec chrooted cmd
        log.debug('Execute chrooted command ...')
        cmd = 'chroot %s %s' % (chrootDir, cmd)
        return systemCall(cmd, sh, log)
    finally:
        # umount if pseudo fs was mounted
        if mountPseudoFs:
            # handle devpts
            if path.exists(devpts):
                umount(devpts, '-lf')
            # lazy is ok for pseudo fs
            umount(dev, '-lf')
            umount(sys, '-lf')
            umount(proc, '-lf')


def chainPathToName(path):
    return path.replace(os.sep, ':')

def chainNameToPath(name):
    return name.replace(':', os.sep)

def loadPyFile(path, ns=None):
    if ns is None:
        ns = {}

    ns['__file__'] = path

    exec open(path).read() in ns

    del ns['__builtins__']

    return ns

def loadChainDefinition(chainName, app=None):
    if app is None:
        app = conduct.app

    # caching
    if 'chains' not in app.cfg:
        app.cfg['chains'] = {}

    if chainName in app.cfg['chains']:
        return app.cfg['chains'][chainName]


    # determine chain file location
    chainDir = app.cfg['chaindefdir']
    chainFile = path.join(chainDir, '%s.py' % chainNameToPath(chainName))

    if not path.exists(chainFile):
        raise IOError('Chain file for \'%s\' not found (Should be: %s)'
                      % (chainName, chainFile))

    # prepare exection namespace
    ns = {
        'Parameter' : Parameter,
        'Step' : lambda cls, **params: ('step:%s' % cls, params),
        'Chain' : lambda cls, **params: ('chain:%s' % cls, params),
        'steps' : OrderedAttrDict(),
    }

    # execute and extract all the interesting data
    ns = loadPyFile(chainFile, ns)

    chainDef = {}

    for entry in ['description', 'parameters']:
        chainDef[entry] = ns[entry]

    chainDef['steps'] = ns['steps']

    # cache
    app.cfg['chains'][chainName] = chainDef

    return chainDef

def loadChainConfig(chainName):
    # determine chain file location
    cfgDir = conduct.app.cfg['chaincfgdir']
    cfgFile = path.join(cfgDir, '%s.py' % chainNameToPath(chainName))

    if path.exists(cfgFile):
        return loadPyFile(cfgFile)
    return {}

def ensureDirectory(dirpath):
    if not path.isdir(dirpath):
        os.makedirs(dirpath)



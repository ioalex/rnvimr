"""
Patch ranger.container.directory.Directory

"""

import os.path
from os import stat as os_stat, lstat as os_lstat
import re
import subprocess
from time import time

from ranger.ext.mount_path import mount_path
from ranger.container.file import File
from ranger.container.directory import InodeFilterConstants
from ranger.container.directory import Directory
from ranger.container.directory import accept_file
from ranger.ext.human_readable import human_readable


def wrap_dir_for_git():
    """
    Wrap directory for hidden git ignored files.

    :param client object: Object of attached neovim session
    """
    Directory.load_bit_by_bit = load_bit_by_bit
    Directory.refilter = refilter
    Directory.load_content_if_outdated = load_content_if_outdated


def _find_git_root(path):
    while True:
        if os.path.basename(path) == '.git':
            return None
        repodir = os.path.join(path, '.git')
        if os.path.exists(repodir):
            return path
        path_o = path
        path = os.path.dirname(path)
        if path == path_o:
            return None


def _walklevel(some_dir, level):
    some_dir = some_dir.rstrip(os.path.sep)
    followlinks = level > 0
    assert os.path.isdir(some_dir)
    num_sep = some_dir.count(os.path.sep)
    for root, dirs, files in os.walk(some_dir, followlinks=followlinks):
        if '.git' in dirs:
            dirs.remove('.git')
        yield root, dirs, files
        num_sep_this = root.count(os.path.sep)
        if level != -1 and num_sep + level <= num_sep_this:
            del dirs[:]


def _mtimelevel(path, level):
    mtime = os.stat(path).st_mtime
    for dirpath, dirnames, _ in _walklevel(path, level):
        dirlist = [os.path.join("/", dirpath, d) for d in dirnames
                   if level == -1 or dirpath.count(os.path.sep) - path.count(os.path.sep) <= level]
        mtime = max(mtime, max([-1] + [os.stat(d).st_mtime for d in dirlist]))
    return mtime


def _build_git_ignore_process(fobj):
    git_root = _find_git_root(fobj.path)
    if git_root:
        grfobj = fobj.fm.get_directory(git_root)
        git_ignore_cmd = ['git', 'status', '--ignored', '-z', '--porcelain', '.']
        if grfobj.load_content_mtime > fobj.load_content_mtime \
                and hasattr(grfobj, 'ignored'):
            fobj.ignored = grfobj.ignored
        else:
            fobj.ignore_proc = subprocess.Popen(git_ignore_cmd, cwd=fobj.path,
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE)


def load_bit_by_bit(self):
    """An iterator that loads a part on every next() call

    Returns a generator which load a part of the directory
    in each iteration.
    """

    self.ignore_proc = None
    if not self.settings.show_hidden and self.settings.hidden_filter:
        _build_git_ignore_process(self)

    self.loading = True
    self.percent = 0
    self.load_if_outdated()

    basename_is_rel_to = self.path if self.flat else None

    try:  # pylint: disable=too-many-nested-blocks
        if self.runnable:
            yield
            mypath = self.path

            self.mount_path = mount_path(mypath)

            if self.flat:
                filelist = []
                for dirpath, dirnames, filenames in _walklevel(mypath, self.flat):
                    dirlist = [
                        os.path.join("/", dirpath, d)
                        for d in dirnames
                        if self.flat == -1
                        or (dirpath.count(os.path.sep)
                            - mypath.count(os.path.sep)) <= self.flat
                    ]
                    filelist += dirlist
                    filelist += [os.path.join("/", dirpath, f) for f in filenames]
                filenames = filelist
                self.load_content_mtime = _mtimelevel(mypath, self.flat)
            else:
                filelist = os.listdir(mypath)
                filenames = [mypath + (mypath == '/' and fname or '/' + fname)
                             for fname in filelist]
                self.load_content_mtime = os.stat(mypath).st_mtime

            if self.cumulative_size_calculated:
                # If self.content_loaded is true, this is not the first
                # time loading.  So I can't really be sure if the
                # size has changed and I'll add a "?".
                if self.content_loaded:
                    if self.fm.settings.autoupdate_cumulative_size:
                        self.look_up_cumulative_size()
                    else:
                        self.infostring = ' %s' % human_readable(
                            self.size, separator='? ')
                else:
                    self.infostring = ' %s' % human_readable(self.size)
            else:
                self.size = len(filelist)
                self.infostring = ' %d' % self.size
            if self.is_link:
                self.infostring = '->' + self.infostring

            yield

            marked_paths = [obj.path for obj in self.marked_items]

            files = []
            disk_usage = 0

            has_vcschild = False
            for name in filenames:
                try:
                    file_lstat = os_lstat(name)
                    if file_lstat.st_mode & 0o170000 == 0o120000:
                        file_stat = os_stat(name)
                    else:
                        file_stat = file_lstat
                except OSError:
                    file_lstat = None
                    file_stat = None
                if file_lstat and file_stat:
                    stats = (file_stat, file_lstat)
                    is_a_dir = file_stat.st_mode & 0o170000 == 0o040000
                else:
                    stats = None
                    is_a_dir = False

                if is_a_dir:
                    item = self.fm.get_directory(name, preload=stats, path_is_abs=True,
                                                 basename_is_rel_to=basename_is_rel_to)
                    item.load_if_outdated()
                    if self.flat:
                        item.relative_path = os.path.relpath(item.path, self.path)
                    else:
                        item.relative_path = item.basename
                    item.relative_path_lower = item.relative_path.lower()
                    if item.vcs and item.vcs.track:
                        if item.vcs.is_root_pointer:
                            has_vcschild = True
                        else:
                            item.vcsstatus = \
                                item.vcs.rootvcs.status_subpath(  # pylint: disable=no-member
                                    os.path.join(self.realpath, item.basename),
                                    is_directory=True,
                                )
                else:
                    item = File(name, preload=stats, path_is_abs=True,
                                basename_is_rel_to=basename_is_rel_to)
                    item.load()
                    disk_usage += item.size
                    if self.vcs and self.vcs.track:
                        item.vcsstatus = \
                            self.vcs.rootvcs.status_subpath(  # pylint: disable=no-member
                                os.path.join(self.realpath, item.basename))

                files.append(item)
                self.percent = 100 * len(files) // len(filenames)
                yield
            self.has_vcschild = has_vcschild
            self.disk_usage = disk_usage

            self.filenames = filenames
            self.files_all = files

            self._clear_marked_items()
            for item in self.files_all:
                if item.path in marked_paths:
                    item.mark_set(True)
                    self.marked_items.append(item)
                else:
                    item.mark_set(False)

            self.sort()

            if files:
                if self.pointed_obj is not None:
                    self.sync_index()
                else:
                    self.move(to=0)
        else:
            self.filenames = None
            self.files_all = None
            self.files = None

        self.cycle_list = None
        self.content_loaded = True
        self.last_update_time = time()
        self.correct_pointer()

    finally:
        self.loading = False
        self.fm.signal_emit("finished_loading_dir", directory=self)
        if self.vcs:
            self.fm.ui.vcsthread.process(self)


def refilter(self):
    if self.files_all is None:
        return  # propably not loaded yet

    self.last_update_time = time()

    filters = []

    if not self.settings.show_hidden and self.settings.hidden_filter:
        hidden_filter = re.compile(self.settings.hidden_filter)
        hidden_filter_search = hidden_filter.search

        def hidden_filter_func(fobj):
            for comp in fobj.relative_path.split(os.path.sep):
                if hidden_filter_search(comp):
                    return False
            return True
        filters.append(hidden_filter_func)

        def exclude_ignore(fobj):
            for rpath in self.ignored:
                if os.path.commonprefix([fobj.path, rpath]) == rpath:
                    return False
            return True

        if self.ignore_proc:
            out, err = self.ignore_proc.communicate()
            if err:
                self.fm.notify(err.decode('utf-8'))
            else:
                self.ignored = [os.path.normpath(os.path.join(_find_git_root(self.path), line[3:]))
                                for line in out.decode('utf-8').split('\0')[:-1]
                                if line.startswith('!! ')]
            self.ignore_proc = None

        if hasattr(self, 'ignored'):
            filters.append(exclude_ignore)

    if self.narrow_filter:
        # pylint: disable=unsupported-membership-test

        # Pylint complains that self.narrow_filter is by default
        # None but the execution won't reach this line if it is
        # still None.
        filters.append(lambda fobj: fobj.basename in self.narrow_filter)
    if self.settings.global_inode_type_filter or self.inode_type_filter:
        def inode_filter_func(obj):
            # Use local inode_type_filter if present, global otherwise
            inode_filter = self.inode_type_filter or self.settings.global_inode_type_filter
            # Apply filter
            if InodeFilterConstants.DIRS in inode_filter and obj.is_directory:
                return True
            if InodeFilterConstants.FILES in inode_filter and obj.is_file and not obj.is_link:
                return True
            if InodeFilterConstants.LINKS in inode_filter and obj.is_link:
                return True
            return False
        filters.append(inode_filter_func)
    if self.filter:
        filter_search = self.filter.search
        filters.append(lambda fobj: filter_search(fobj.basename))
    if self.temporary_filter:
        temporary_filter_search = self.temporary_filter.search
        filters.append(lambda fobj: temporary_filter_search(fobj.basename))
    filters.extend(self.filter_stack)

    self.files = [f for f in self.files_all if accept_file(f, filters)]

    # A fix for corner cases when the user invokes show_hidden on a
    # directory that contains only hidden directories and hidden files.
    if self.files and not self.pointed_obj:
        self.pointed_obj = self.files[0]
    elif not self.files:
        self.content_loaded = False
        self.pointed_obj = None

    self.move_to_obj(self.pointed_obj)


def load_content_if_outdated(self, *a, **k):
    """Load the contents of the directory if outdated"""

    if self.load_content_once(*a, **k):
        return True

    if self.files_all is None or self.content_outdated:
        self.load_content(*a, **k)
        return True

    try:
        if self.flat:
            real_mtime = _mtimelevel(self.path, self.flat)
        else:
            real_mtime = os.stat(self.path).st_mtime
    except OSError:
        real_mtime = None
        return False
    if self.stat:
        cached_mtime = self.load_content_mtime
    else:
        cached_mtime = 0

    if real_mtime != cached_mtime:
        self.load_content(*a, **k)
        return True
    return False

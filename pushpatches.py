#!/usr/bin/python3

"""Push patches to the FreeIPA repository

Usage:
  pushpatches.py [options] [-v...] [--branch=BRANCH...] [--reviewer=NAME...] [--] [<patch> ...]

Options:
  -h, --help           Display this help and exit
  -v, --verbose        Increase verbosity
  --config FILE        Configuration file [default: ~/.ipa/pushpatch.yaml]
  -b, --branch=BRANCH  Branch to push to (detected from ticket if no --branch is given)
  -r, --reviewer=NAME  Reviewer of the patches
  --no-reviewer        Do not add a Reviewed-By: line
  -n, --dry-run        Do not push
  --no-trac            Do not contact Trac
  --no-fetch           Do not synchronize before pushing
  --color=(auto|always|never)  Colorize output [default: auto]
  <patch>              Patch to push, or directory with *.patch files

The given patches are applied on top of the given upstream branches,
and pushed.
A "Reviewed-By" line with the name given by --reviewer is added to all patches
unless --no-reviewer is given).
If neither --reviewer nor --no-reviewer is given, a reviewer is looked up in
the "Reviewed by" field of the Trac ticket(s). If that does not yield one
reviewer, the pushpatches command fails..
If the reviewer name is not in the
form "Name Last <mail@address.example>", it is looked up in the contributors
as listed in `git shortlog -se`.

Configuration can be specified in the file given by --config.
Here is an example
  patchdir: ~/patches/to-apply
  clean-repo-path: ~/dev/freeipa-clean
  ticket-url: https://fedorahosted.org/freeipa/ticket/
  commit-url: https://fedorahosted.org/freeipa/changeset/
  bugzilla-bug-url: https://bugzilla.redhat.com/show_bug.cgi?id=
  trac-xmlrpc-url: https://fedorahosted.org/freeipa/xmlrpc
  remote: origin
  browser: firefox
"""

import glob
import os
import subprocess
import re
import collections
import xmlrpc.client
import pprint

import docopt     # yum install python3-docopt
import yaml       # yum install python3-PyYAML
import blessings  # yum install python3-blessings
import unidecode  # yum install python3-unidecode

MILESTONES = {
    "^FreeIPA 3\.4 .*": ['master'],
    "^FreeIPA 3\.3\..*": ['master', 'ipa-3-3']
}

COLOR_OPT_MAP = {'auto': False, 'always': True, 'never': None}

SUBJECT_RE = re.compile('^Subject:( *\[PATCH( [^]*])?\])*(?P<subj>.*)')

SubprocessResult = collections.namedtuple(
    'SubprocessResult', 'stdout stderr returncode')
TracTicketData = collections.namedtuple(
    'TracTicketData', 'id time_created time_changed attributes')
OidEntry = collections.namedtuple(
    'OidEntry', 'oid name')

def cleanpath(path):
    """Return absolute path with leading ~ expanded"""
    path = os.path.expanduser(path)
    path = os.path.abspath(path)
    return path


def shellquote(arg):
    """Quote an argument for the shell"""
    if re.match('^[-_.:/=a-zA-Z0-9]*$', arg):
        return arg
    else:
        return "'%s'" % arg.replace("'", r"'\''")


class reify(object):
    # https://github.com/Pylons/pyramid/blob/1.4-branch/pyramid/decorator.py
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.__doc__ = getattr(wrapped, '__doc__', None)

    def __get__(self, inst, objtype=None):
        if inst is None:
            return self
        val = self.wrapped(inst)
        setattr(inst, self.wrapped.__name__, val)
        return val


class Patch(object):
    """Represents a sanitized patch

    - Removes ">" from From lines in the metadata/message
    - Adds a Reviewed-By tag

    Attributes:
    * subject - name of the patch
    * lines - iterator of lines with the patch
    * ticket_numbers - numbers of referenced Trac tickets
    """
    def __init__(self, config, filename):
        if filename:
            self.filename = filename
            with open(filename) as file:
                lines = list(file)
        else:
            self.filename = '(patch)'
        assert lines
        lines_iter = iter(lines)
        self.head_lines = []
        self.patch_lines = []
        in_subject = False
        self.subject = ''
        for line in lines_iter:
            if not line.startswith(' '):
                in_subject = False
            if in_subject:
                self.subject += line.rstrip()
            match = SUBJECT_RE.match(line)
            if match:
                self.subject = match.group('subj').strip()
                in_subject = True

            if line.startswith('>From'):
                self.head_lines.append(line[1:])
            elif any([
                    line == '---\n',
                    line.startswith('diff -'),
                    line.startswith('Index: ')]):
                self.patch_lines.append(line)
                break
            else:
                self.head_lines.append(line)
        self.patch_lines.extend(lines_iter)

        self.ticket_numbers = []
        for line in self.lines:
            regex = '%s(\d*)' % re.escape(config['ticket-url'])
            for match in re.finditer(regex, line):
                self.ticket_numbers.append(int(match.group(1)))

    def add_reviewer(self, reviewer):
        if not re.match('^[-_a-zA-Z0-9]+: .*$', self.head_lines[-1]):
            self.head_lines.append('\n')
        self.head_lines.append('Reviewed-By: %s\n' % reviewer)

    @property
    def modified_oids(self):
        oid_regex = ("^\+(attributeTypes|objectClasses): "
                     "\((?P<oid>[\d.]+) NAME '(?P<name>\w+)")
        data = ''.join(self.patch_lines)
        for match in re.finditer(oid_regex, data, re.MULTILINE):
            yield OidEntry(match.group('oid'), match.group('name'))

    @property
    def lines(self):
        yield from self.head_lines
        yield from self.patch_lines


class Ticket(object):
    """Trac ticket with lazily fetched information"""
    def __init__(self, trac, number):
        self.trac = trac
        self.number = number

    @reify
    def data(self):
        print('Retrieving ticket %s' % self.number)
        data = TracTicketData(*self.trac.ticket.get(self.number))
        return data

    @property
    def attributes(self):
        return self.data.attributes


class Pusher(object):
    # This is a class (as opposed to function) because it holds a bunch of
    # common configuration- and output-related attributes:
    # * options (dict from CLI arguments)
    # * config (dict from config file)
    # * term (a Blessings terminal)
    # * trac (a Trac XML-RPC ServerProxy, or None)
    # * color_arg (X to pass to git --color=X to get colored output)
    def __init__(self, options):
        self.options = options
        with open(os.path.expanduser(options['--config'])) as conf_file:
            self.config = yaml.safe_load(conf_file)
        self.term = blessings.Terminal(
            force_styling=COLOR_OPT_MAP[options['--color']])
        self.verbosity = self.options['--verbose']
        if self.verbosity:
            print('Options:')
            pprint.pprint(self.options)
            print('Config:')
            pprint.pprint(self.config)
        if self.options['--no-trac']:
            self.trac = None
        else:
            url = self.config['trac-xmlrpc-url']
            self.trac = xmlrpc.client.ServerProxy(url)

        self.color_arg = self.options['--color']
        if self.color_arg == 'auto':
            if self.term.is_a_tty:
                self.color_arg = 'always'
            else:
                self.color_arg = 'never'

    def die(self, message):
        print(self.term.red(message))
        exit(1)

    def get_patches(self):
        paths = self.options['<patch>'] or [self.config['patchdir']]
        for path in paths:
            path = cleanpath(path)
            if os.path.isdir(path):
                filenames = glob.glob(os.path.join(path, '*.patch'))
                for filename in sorted(filenames):
                    yield Patch(self.config, filename)
            else:
                yield Patch(self.config, path)

    def git(self, *argv, **kwargs):
        """Run a git command"""
        return self.runcommand('git', *argv, **kwargs)

    def runcommand(self, *argv, check_stdout=None, check_stderr=None,
                   check_returncode=0, stdin_string='', fail_message=None,
                   timeout=5, verbosity=None):
        """Run a command in a subprocess, check & return result"""
        argv_repr = ' '.join(shellquote(a) for a in argv)
        if verbosity is None:
            verbosity = self.verbosity
        if verbosity:
            print(self.term.blue(argv_repr))
        if verbosity > 2:
            print(self.term.yellow(stdin_string.rstrip()))
        PIPE = subprocess.PIPE
        proc = subprocess.Popen(argv, stdout=PIPE, stderr=PIPE, stdin=PIPE)
        try:
            stdout, stderr = proc.communicate(stdin_string.encode('utf-8'),
                                             timeout=timeout)
            timeout_expired = False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout = stderr = b''
            timeout_expired = True
        stdout = stdout.decode('utf-8')
        stderr = stderr.decode('utf-8')
        returncode = proc.returncode
        failed = any([
            timeout_expired,
            (check_stdout is not None and check_stdout != stdout),
            (check_stderr is not None and check_stderr != stderr),
            (check_returncode is not None and check_returncode != returncode),
        ])
        if failed and not verbosity:
            print(self.term.blue(argv_repr))
        if failed or verbosity >= 2:
            if stdout:
                print(stdout.rstrip())
            if stderr:
                print(self.term.yellow(stderr.rstrip()))
            print('→ %s' % self.term.blue(str(proc.returncode)))
        if failed:
            if timeout_expired:
                self.die('Git command timeout expired')
            elif fail_message:
                self.die(fail_message)
            else:
                self.die('Git command failed')
        return SubprocessResult(stdout, stderr, returncode)

    def ensure_clean_repo(self, git_dir=None, work_tree=None):
        """Make sure the working tree matches the git index"""
        args = ['--git-dir', git_dir] if git_dir else []
        args += ['--work-tree', work_tree] if work_tree else []
        args += ['status', '--porcelain']

        self.git(*args,
                 check_stdout='',
                 check_stderr='',
                 fail_message='Repository %s not clean'
                              % git_dir or os.getcwd())

    def get_rewiewers(self, tickets):
        """Get reviewer name & address, or None for --no-reviewer

        Raises if a suitable reviewer is not found
        """
        if self.options['--no-reviewer']:
            return None
        reviewers = self.options['--reviewer']
        if self.trac and not reviewers:
            reviewers = set()
            for ticket in tickets:
                if ticket.attributes.get('reviewer'):
                    reviewers.add(ticket.attributes['reviewer'])
            if len(reviewers) > 1:
                print('Reviewers found: %s' % ', '.join(reviewers))
                self.die('Too many reviewers found in ticket(s), '
                         'specify --reviewer explicitly')
            if not reviewers:
                self.die('No reviewer found in ticket(s), '
                         'specify --reviewer explicitly')
        if not reviewers:
            self.die('No reviewer found, please specify --reviewer')

        normalized = set()
        for reviewer in reviewers:
            normalized.add(self.normalize_reviewer(reviewer))
        return normalized

    def normalize_reviewer(self, reviewer):
        name_re = re.compile(r'^\w+ [^<]+ <.*@.*\..*>$')
        if name_re.match(reviewer):
            return reviewer
        rbranch = '%s/master' % self.config['remote']
        names = self.git('shortlog', '-sen', rbranch).stdout.splitlines()
        names = (name.split('\t', 1)[-1] for name in names)
        names = (name for name in names if name_re.match(name))
        names = [name for name in names if reviewer.lower() in name.lower()]
        if not names:
            self.die('Reviewer %s not found' % reviewer)
        elif len(names) > 1:
            print(self.term.red('Reviewer %s could be:' % reviewer))
            for name in names:
                print('- %s' % name)
            self.die('Multiple matches found for reviewer')
        else:
            name = unidecode.unidecode(names[0])
            return name

    def apply_patches(self, patches, branch):
        """Apply patches to the given branch

        Checks out the branch
        """
        self.git('checkout', '%s/%s' % (self.config['remote'], branch))
        for patch in patches:
            print('Aplying to %s: %s' % (branch, patch.subject))
            self.git('am', stdin_string=''.join(patch.lines))
        sha1 = self.git('rev-parse', 'HEAD').stdout.strip()
        if self.verbosity:
            print('Resulting hash: %s' % sha1)
        return sha1

    def is_oid_registered(self, oid_entry):
        oid_git = cleanpath(self.config.get('oid-git'))
        oid = oid_entry.oid
        name = oid_entry.name

        if oid_git:
            oid_git_path = os.path.join(oid_git, '.git/')
            self.ensure_clean_repo(git_dir=oid_git_path, work_tree=oid_git)

            result = self.git('--git-dir', oid_git_path,
                              '--work-tree', oid_git,
                              'grep', oid,
                              check_returncode=None)
            if result.returncode != 0:
                print("OID %s (%s) is not registered" % (oid, name))
                return False
            elif name not in result.stdout:
                print("OID %s (%s) clashes with:" % (oid, name))
                print(result.stdout)
                return False
            else:
                print("OID %s (%s) correctly registered" % (oid, name))
                return True

    def print_push_info(self, patches, sha1s, ticket_numbers, tickets):
        """Print lots of info about the to-be-pushed commits"""
        remote = self.config['remote']
        branches = sha1s.keys()

        trac_log = []
        bugzilla_log = ['Fixed upstream']
        for branch in branches:
            trac_log.append('%s:' % branch)
            bugzilla_log.append('%s:' % branch)
            log_result = self.git(
                'log', '--graph', '--oneline', '--abbrev=99',
                '--color=%s' % self.color_arg,
                '%s/%s..%s' % (remote, branch, sha1s[branch]))
            trac_log.extend(
                line.rstrip()
                for line in reversed(log_result.stdout.splitlines()))

            log_result = self.git(
                'log', '--pretty=format:%H',
                '%s/%s..%s' % (remote, branch, sha1s[branch]))
            bugzilla_log.extend(
                self.config['commit-url'] + line.strip()
                for line in reversed(log_result.stdout.splitlines()))

        bugzilla_urls = []
        bugzilla_re = re.compile('(%s\d+)' %
                                 re.escape(self.config['bugzilla-bug-url']))
        for ticket in tickets:
            for match in bugzilla_re.finditer(ticket.attributes['rhbz']):
                bugzilla_urls.append(match.group(0))

        for branch in branches:
            print(self.term.cyan('=== Log for %s ===' % branch))
            log_result = self.git(
                'log', '--reverse', '--color=%s' % self.color_arg,
                '%s/%s..%s' % (remote, branch, sha1s[branch]),
                verbosity=1)
            if self.verbosity < 2:
                print(log_result.stdout)

        print(self.term.cyan('=== Patches pushed ==='))
        for patch in patches:
            print(patch.filename)

        print(self.term.cyan('=== Mail summary ==='))
        if len(branches) == 1:
            print('Pushed to ', end='')
        else:
            print('Pushed to:')
        for branch in branches:
            print('%s: %s' % (branch, sha1s[branch]))

        print(self.term.cyan('=== Trac comment ==='))
        print('\n'.join(trac_log))

        print(self.term.cyan('=== Bugzilla comment ==='))
        print('\n'.join(bugzilla_log))

        if ticket_numbers:
            print(self.term.cyan('=== Tickets fixed ==='))
            for number in sorted(ticket_numbers):
                print('%s%s' % (self.config['ticket-url'], number))

        if bugzilla_urls:
            print(self.term.cyan('=== Bugzillas fixed ==='))
            print('\n'.join(bugzilla_urls))

        print(self.term.cyan('=== Ready to push ==='))

    def run(self):
        os.chdir(cleanpath(self.config['clean-repo-path']))
        self.ensure_clean_repo()

        patches = list(self.get_patches())
        if not patches:
            self.die('No patches to push')

        ticket_numbers = set()
        for patch in patches:
            ticket_numbers.update(patch.ticket_numbers)
        if self.trac:
            tickets = [Ticket(self.trac, n) for n in ticket_numbers]
        else:
            tickets = []

        reviewers = self.get_rewiewers(tickets)
        if reviewers:
            for reviewer in reviewers:
                print('Reviewer: %s' % reviewer)
                for patch in patches:
                    patch.add_reviewer(reviewer)
        else:
            print('Reviewer: None')

        # Check the OIDs
        if not self.options.get('--no-oid-check', False):
            if self.config.get('oid-git'):
                missing_oids = [oid_entry for p in patches
                                for oid_entry in p.modified_oids
                                if not self.is_oid_registered(oid_entry)]
                if missing_oids:
                    self.die('OIDs in the patcheset are not registered')
            else:
                print('The oid-git option not found, skipping the OID check.')

        branches = self.options['--branch']
        if not branches:
            if not tickets:
                if self.trac:
                    self.die('No branches specified and no tickets found')
                else:
                    self.die('No branches specified and trac disabled')
            if self.verbosity:
                print('Divining branches from tickets: %s' %
                        ', '.join(str(t.number) for t in tickets))
            milestones = set(t.attributes['milestone'] for t in tickets)
            if not milestones:
                self.die('No milestones found in tickets')
            elif len(milestones) > 1:
                self.die('Tickets belong to disparate milestones; '
                         'fix them in Trac or specify branches explicitly')
            [milestone] = milestones
            for template, templ_branches in MILESTONES.items():
                if re.match(template, milestone):
                    branches = templ_branches
                    break
            else:
                self.die('No branches correspond to `%s`. ' % milestone +
                         'Update MILESTONES in the pushpatches script.')
        print('Will apply %s patches to: %s' %
              (len(patches), ', '.join(branches)))

        remote = self.config['remote']

        if not self.options['--no-fetch']:
            print('Fetching...')
            self.git('fetch',remote, timeout=60)

        rev_parse = self.git('rev-parse', '--abbrev-ref', 'HEAD')
        old_branch = rev_parse.stdout.strip()
        if self.verbosity:
            print('Old branch: %s' % old_branch)
        try:
            sha1s = collections.OrderedDict()
            for branch in branches:
                sha1s[branch] = self.apply_patches(patches, branch)

            push_args = ['%s:%s' % (sha1, branch)
                         for branch, sha1 in sha1s.items()]
            print('Trying push...')
            self.git('push', '--dry-run', remote, *push_args,
                     timeout=60, verbosity=2)

            print('Generating info...')
            self.print_push_info(patches, sha1s, ticket_numbers, tickets)

            if self.options['--dry-run']:
                print('Exiting, --dry-run specified')
            else:
                while True:
                    print('(k will start `gitk`)')
                    branchesrepr = ', '.join(branches)
                    response = input('Push to %s? [y/n/k] ' % branchesrepr)
                    if response.lower() == 'n':
                        break
                    elif response.lower() == 'k':
                        self.runcommand('gitk',
                                        *(branches + list(sha1s.values())),
                                        timeout=None)
                    elif response.lower() == 'y':
                        print('Pushing')
                        self.git('push', remote, *push_args,
                                timeout=60, verbosity=2)
                        break

        finally:
            print('Cleaning up')
            self.git('am', '--abort', check_returncode=None)
            self.git('reset', '--hard', check_returncode=None)
            self.git('checkout', old_branch, check_returncode=None)
            self.git('clean', '-fxd', check_returncode=None)


if __name__ == '__main__':
    Pusher(docopt.docopt(__doc__)).run()

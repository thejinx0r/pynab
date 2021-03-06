import tempfile
import os
import re
import shutil
import subprocess

import lib.rar
from pynab import log
from pynab.db import db
import pynab.nzbs
import pynab.releases
from pynab.server import Server
import config

MAYBE_PASSWORDED_REGEX = re.compile('\.(ace|cab|tar|gz|url)$', re.I)
PASSWORDED_REGEX = re.compile('password\.url', re.I)


def check_rar(filename):
    """Determines whether a rar is passworded or not.
    Returns either a list of files (if the file is a rar and unpassworded),
    False if it's not a RAR, and True if it's a passworded/encrypted RAR.
    """
    try:
        rar = lib.rar.RarFile(filename)
    except:
        # wasn't a rar
        raise lib.rar.BadRarFile

    if rar:
        # was a rar! check for passworded inner rars
        if any([r.is_encrypted for r in rar.infolist()]):
            return False
        else:
            return rar.infolist()
    else:
        # probably an encrypted rar!
        return False


def get_rar_info(server, group_name, messages):
    data = server.get(group_name, messages)

    if data:
        # if we got the requested articles, save them to a temp rar
        t = None
        with tempfile.NamedTemporaryFile('wb', suffix='.rar', delete=False) as t:
            t.write(data.encode('ISO-8859-1'))
            t.flush()

        try:
            files = check_rar(t.name)
        except lib.rar.BadRarFile:
            log.debug('Deleting temp files...')
            os.remove(t.name)
            return None

        # build a summary to return

        info = {
            'files.count': 0,
            'files.size': 0,
            'files.names': []
        }

        passworded = False
        if files:
            info = {
                'files.count': len(files),
                'files.size': sum([r.file_size for r in files]),
                'files.names': [r.filename for r in files]
            }

            # make a tempdir to extract rar to
            tmp_dir = tempfile.mkdtemp()
            log.debug('Creating temp directory: {}...'.format(tmp_dir))
            exe = [
                '"{}"'.format(config.site['unrar_path']),
                'e', '-ai', '-ep', '-r', '-kb',
                '-c-', '-id', '-p-', '-y', '-inul',
                '"{}"'.format(t.name),
                '"{}"'.format(tmp_dir)
            ]

            try:
                subprocess.check_call(' '.join(exe), stderr=subprocess.STDOUT, shell=True)
            except subprocess.CalledProcessError as cpe:
                log.debug('Archive had issues while extracting: {}: {} {}'.format(cpe.cmd, cpe.returncode, cpe.output))
                log.debug('Not to worry, it\'s probably a multi-volume rar (most are).')
                log.debug(info)

            inner_passwords = []
            for file in files:
                fpath = os.path.join(tmp_dir, file.filename)
                try:
                    inner_files = check_rar(fpath)
                except lib.rar.BadRarFile:
                    log.debug('Inner file {} wasn\'t a RAR archive.'.format(file.filename))
                    continue

                if inner_files:
                    inner_passwords += [r.is_encrypted for r in inner_files]
                else:
                    passworded = True
                    break

            if not passworded:
                passworded = any(inner_passwords)

            log.debug('Deleting temp files...')
            os.remove(t.name)
            shutil.rmtree(tmp_dir)
        else:
            log.debug('Archive was encrypted or passworded.')
            passworded = True

        info['passworded'] = passworded

        return info


def check_release_files(server, group_name, nzb):
    """Retrieves rar metadata for release files."""

    for rar in nzb['rars']:
        messages = []
        if not isinstance(rar['segments']['segment'], list):
            rar['segments']['segment'] = [rar['segments']['segment'], ]
        for s in rar['segments']['segment']:
            messages.append(s['#text'])
            break

        if messages:
            info = get_rar_info(server, group_name, messages)

            if info and not info['passworded']:
                passworded = False
                for file in info['files.names']:
                    result = MAYBE_PASSWORDED_REGEX.search(file)
                    if result:
                        passworded = 'potentially'
                        break

                    result = PASSWORDED_REGEX.search(file)
                    if result:
                        passworded = True
                        break

                if passworded:
                    info['passworded'] = passworded

            return info

    return None


def process(limit=20):
    """Processes release rarfiles to check for passwords and filecounts. Optionally
    deletes passworded releases."""
    log.info('Checking for passworded releases and deleting them if appropriate...')

    with Server() as server:
        for release in db.releases.find({'passworded': None}).limit(limit):
            log.debug('Processing rar part for {}...'.format(release['name']))
            nzb = pynab.nzbs.get_nzb_dict(release['nzb'])

            if nzb['rars']:
                info = check_release_files(server, release['group']['name'], nzb)
                if info:
                    log.info('Adding file data to release: {}'.format(release['name']))
                    db.releases.update({'_id': release['_id']}, {
                        '$set': {
                            'files.count': info['files.count'],
                            'files.size': info['files.size'],
                            'files.names': info['files.names'],
                            'passworded': info['passworded']
                        }
                    })

                    continue

            log.debug('No RARs in release, blacklisting...')
            db.releases.update({'_id': release['_id']}, {
                '$set': {
                    'files.count': 0,
                    'files.size': 0,
                    'files.names': [],
                    'passworded': 'unknown'
                }
            })

    if config.site['delete_passworded']:
        log.info('Deleting passworded releases...')
        if config.site['delete_potentially_passworded']:
            query = {'passworded': {'$in': [True, 'potentially']}}
        else:
            query = {'passworded': True}
        db.releases.remove(query)
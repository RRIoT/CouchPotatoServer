import json
import os
import time
import traceback
from couchpotato import CPLog
from couchpotato.api import addApiView
from couchpotato.core.event import addEvent, fireEvent
from couchpotato.core.helpers.encoding import toUnicode
from couchpotato.core.helpers.variable import getImdb

log = CPLog(__name__)


class Database(object):

    indexes = []
    db = None

    def __init__(self):

        addApiView('database.list_documents', self.listDocuments)
        addApiView('database.document.update', self.updateDocument)
        addApiView('database.document.delete', self.deleteDocument)

        addEvent('database.setup_index', self.setupIndex)
        addEvent('app.migrate', self.migrate)

    def getDB(self):

        if not self.db:
            from couchpotato import get_db
            self.db = get_db()

        return self.db

    def setupIndex(self, index_name, klass):

        self.indexes.append(index_name)

        db = self.getDB()

        # Category index
        try:
            db.add_index(klass(db.path, index_name))
            db.reindex_index(index_name)
        except:
            previous_version = db.indexes_names[index_name]._version
            current_version = klass._version

            # Only edit index if versions are different
            if previous_version < current_version:
                log.debug('Index "%s" already exists, updating and reindexing', index_name)
                db.edit_index(klass(db.path, index_name), reindex = True)

    def deleteDocument(self, **kwargs):

        db = self.getDB()

        try:

            document_id = kwargs.get('_request').get_argument('id')
            document = db.get('id', document_id)
            db.delete(document)

            return {
                'success': True
            }
        except:
            return {
                'success': False,
                'error': traceback.format_exc()
            }

    def updateDocument(self, **kwargs):

        db = self.getDB()

        try:

            document = json.loads(kwargs.get('_request').get_argument('document'))
            d = db.update(document)
            document.update(d)

            return {
                'success': True,
                'document': document
            }
        except:
            return {
                'success': False,
                'error': traceback.format_exc()
            }

    def listDocuments(self, **kwargs):
        db = self.getDB()

        results = {
            'unknown': []
        }

        for document in db.all('id'):
            key = document.get('_t', 'unknown')

            if kwargs.get('show') and key != kwargs.get('show'):
                continue

            if not results.get(key):
                results[key] = []
            results[key].append(document)


        return results

    def migrate(self):

        from couchpotato import Env
        old_db = os.path.join(Env.get('data_dir'), 'couchpotato.db')
        if not os.path.isfile(old_db): return

        log.info('=' * 30)
        log.info('Migrating database, hold on..')
        time.sleep(1)

        if os.path.isfile(old_db):

            migrate_start = time.time()

            import sqlite3
            conn = sqlite3.connect(old_db)

            migrate_list = {
                'category': ['id', 'label', 'order', 'required', 'preferred', 'ignored', 'destination'],
                'profile': ['id', 'label', 'order', 'core', 'hide'],
                'profiletype': ['id', 'order', 'finish', 'wait_for', 'quality_id', 'profile_id'],
                'quality': ['id', 'identifier', 'order', 'size_min', 'size_max'],
                'movie': ['id', 'last_edit', 'library_id', 'status_id', 'profile_id', 'category_id'],
                'library': ['id', 'identifier', 'info'],
                'librarytitle': ['id', 'title', 'default', 'libraries_id'],
                'library_files__file_library': ['library_id', 'file_id'],
                'release': ['id', 'identifier', 'movie_id', 'status_id', 'quality_id', 'last_edit'],
                'releaseinfo': ['id', 'identifier', 'value', 'release_id'],
                'status': ['id', 'identifier'],
                'properties': ['id', 'identifier', 'value'],
                'file': ['id', 'path', 'type_id'],
                'filetype': ['identifier', 'id']
            }

            migrate_data = {}

            c = conn.cursor()

            for ml in migrate_list:
                migrate_data[ml] = {}
                rows = migrate_list[ml]
                c.execute('SELECT %s FROM `%s`' % ('`' + '`,`'.join(rows) + '`', ml))

                for p in c.fetchall():
                    columns = {}
                    for row in migrate_list[ml]:
                        columns[row] = p[rows.index(row)]

                    if not migrate_data[ml].get(p[0]):
                        migrate_data[ml][p[0]] = columns
                    else:
                        if not isinstance(migrate_data[ml][p[0]], list):
                            migrate_data[ml][p[0]] = [migrate_data[ml][p[0]]]
                        migrate_data[ml][p[0]].append(columns)

            c.close()

            log.info('Getting data took %s', time.time() - migrate_start)

            db = self.getDB()

            # Use properties
            properties = migrate_data['properties']
            log.info('Importing %s properties', len(properties))
            for x in properties:
                property = properties[x]
                Env.prop(property.get('identifier'), property.get('value'))

            # Categories
            categories = migrate_data.get('category', [])
            log.info('Importing %s categories', len(categories))
            category_link = {}
            for x in categories:
                c = categories[x]

                new_c = db.insert({
                    '_t': 'category',
                    'order': c.get('order', 999),
                    'label': toUnicode(c.get('label', '')),
                    'ignored': toUnicode(c.get('ignored', '')),
                    'preferred': toUnicode(c.get('preferred', '')),
                    'required': toUnicode(c.get('required', '')),
                    'destination': toUnicode(c.get('destination', '')),
                })

                category_link[x] = new_c.get('_id')

            # Profiles
            log.info('Importing profiles')
            new_profiles = db.all('profile', with_doc = True)
            new_profiles_by_label = {}
            for x in new_profiles:

                # Remove default non core profiles
                if not x['doc'].get('core'):
                    db.delete(x['doc'])
                else:
                    new_profiles_by_label[x['doc']['label']] = x['_id']

            profiles = migrate_data['profile']
            profile_link = {}
            for x in profiles:
                p = profiles[x]

                exists = new_profiles_by_label.get(p.get('label'))

                # Update existing with order only
                if exists and p.get('core'):
                    profile = db.get('id', exists)
                    profile['order'] = p.get('order')
                    db.update(profile)

                    profile_link[x] = profile.get('_id')
                else:

                    new_profile = {
                        '_t': 'profile',
                        'label': p.get('label'),
                        'order': int(p.get('order', 999)),
                        'core': p.get('core', False),
                        'qualities': [],
                        'wait_for': [],
                        'finish': []
                    }

                    types = migrate_data['profiletype']
                    for profile_type in types:
                        p_type = types[profile_type]
                        if types[profile_type]['profile_id'] == p['id']:
                            new_profile['finish'].append(p_type['finish'])
                            new_profile['wait_for'].append(p_type['wait_for'])
                            new_profile['qualities'].append(migrate_data['quality'][p_type['quality_id']]['identifier'])

                    new_profile.update(db.insert(new_profile))

                    profile_link[x] = new_profile.get('_id')

            # Qualities
            log.info('Importing quality sizes')
            new_qualities = db.all('quality', with_doc = True)
            new_qualities_by_identifier = {}
            for x in new_qualities:
                new_qualities_by_identifier[x['doc']['identifier']] = x['_id']

            qualities = migrate_data['quality']
            quality_link = {}
            for x in qualities:
                q = qualities[x]
                q_id = new_qualities_by_identifier[q.get('identifier')]

                quality = db.get('id', q_id)
                quality['order'] = q.get('order')
                quality['size_min'] = q.get('size_min')
                quality['size_max'] = q.get('size_max')
                db.update(quality)

                quality_link[x] = quality

            # Titles
            titles = migrate_data['librarytitle']
            titles_by_library = {}
            for x in titles:
                title = titles[x]
                if title.get('default'):
                    titles_by_library[title.get('libraries_id')] = title.get('title')

            # Releases
            releaseinfos = migrate_data['releaseinfo']
            for x in releaseinfos:
                info = releaseinfos[x]
                if not migrate_data['release'][info.get('release_id')].get('info'):
                    migrate_data['release'][info.get('release_id')]['info'] = {}

                migrate_data['release'][info.get('release_id')]['info'][info.get('identifier')] = info.get('value')

            releases = migrate_data['release']
            releases_by_media = {}
            for x in releases:
                release = releases[x]
                if not releases_by_media.get(release.get('movie_id')):
                    releases_by_media[release.get('movie_id')] = []

                releases_by_media[release.get('movie_id')].append(release)

            # Media
            log.info('Importing %s media items', len(migrate_data['movie']))
            statuses = migrate_data['status']
            libraries = migrate_data['library']
            library_files = migrate_data['library_files__file_library']
            all_files = migrate_data['file']
            poster_type = migrate_data['filetype']['poster']
            medias = migrate_data['movie']
            for x in medias:
                m = medias[x]

                status = statuses.get(m['status_id']).get('identifier')
                l = libraries[m['library_id']]

                # Only migrate wanted movies, Skip if no identifier present
                if not getImdb(l.get('identifier')): continue

                profile_id = profile_link.get(m['profile_id'])
                category_id = category_link.get(m['category_id'])
                title = titles_by_library.get(m['library_id'])
                releases = releases_by_media.get(x, [])
                info = json.loads(l.get('info', ''))

                files = library_files.get(m['library_id'], [])
                if not isinstance(files, list):
                    files = [files]

                added_media = fireEvent('movie.add', {
                    'info': info,
                    'identifier': l.get('identifier'),
                    'profile_id': profile_id,
                    'category_id': category_id,
                    'title': title
                },  force_readd = False, search_after = False, update_after = False, notify_after = False, status = status, single = True)
                added_media['files'] = added_media.get('files', {})

                for f in files:
                    ffile = all_files[f.get('file_id')]

                    # Only migrate posters
                    if ffile.get('type_id') == poster_type.get('id'):
                        if ffile.get('path') not in added_media['files'].get('image_poster', []) and os.path.isfile(ffile.get('path')):
                            added_media['files']['image_poster'] = [ffile.get('path')]
                            break

                db.update(added_media)

                for rel in releases:
                    if not rel.get('info'): continue

                    quality = quality_link[rel.get('quality_id')]

                    # Add status to keys
                    rel['info']['status'] = statuses.get(rel.get('status_id')).get('identifier')
                    fireEvent('release.create_from_search', [rel['info']], added_media, quality, single = True)

            # rename old database
            log.info('Renaming old database to %s ', old_db + '.old')
            os.rename(old_db, old_db + '.old')

            if os.path.isfile(old_db + '-wal'):
                os.rename(old_db + '-wal', old_db + '-wal.old')
            if os.path.isfile(old_db + '-shm'):
                os.rename(old_db + '-shm', old_db + '-shm.old')

            log.info('Total migration took %s', time.time() - migrate_start)
            log.info('=' * 30)

"""
Tests for synchronization tools.
"""
from __future__ import unicode_literals
from base64 import b64encode
from logging import Logger
from unittest import TestCase

from cryptography.fernet import Fernet
from sqlalchemy import create_engine

from pgcontents import PostgresContentsManager
from pgcontents.crypto import (
    FernetEncryption,
    NoEncryption,
    single_password_crypto_factory,
)
from pgcontents.query import generate_files, generate_checkpoints
from pgcontents.utils.ipycompat import new_markdown_cell

from .utils import (
    assertRaisesHTTPError,
    clear_test_db,
    remigrate_test_schema,
    populate,
    TEST_DB_URL,
)
from ..utils.sync import (
    reencrypt_all_users,
    unencrypt_all_users,
)


class TestReEncryption(TestCase):

    def setUp(self):
        remigrate_test_schema()

    def tearDown(self):
        clear_test_db()

    def add_markdown_cell(self, path):
        # Load and update
        model = self.contents.get(path=path)
        model['content'].cells.append(
            new_markdown_cell('Created by test: ' + path)
        )

        # Save and checkpoint again.
        self.contents.save(model, path=path)
        return model

    def test_reencryption(self):
        """
        Create two unencrypted notebooks and a file, create checkpoints for
        each, then encrypt and check that content is unchanged, then re-encrypt
        and check the same.
        """
        db_url = TEST_DB_URL
        user_id = 'test_reencryption'

        no_crypto = NoEncryption()
        no_crypto_manager = PostgresContentsManager(
            user_id=user_id,
            db_url=db_url,
            crypto=no_crypto,
            create_user_on_startup=True,
        )

        key1 = b'fizzbuzz' * 4
        crypto1 = FernetEncryption(Fernet(b64encode(key1)))
        manager1 = PostgresContentsManager(
            user_id=user_id,
            db_url=db_url,
            crypto=crypto1,
        )

        key2 = key1[::-1]
        crypto2 = FernetEncryption(Fernet(b64encode(key2)))
        manager2 = PostgresContentsManager(
            user_id=user_id,
            db_url=db_url,
            crypto=crypto2,
        )

        # Populate an unencrypted user.
        paths = populate(no_crypto_manager)

        original_content = {}
        for path in paths:
            # Create a checkpoint of the original content and store what we
            # expect it to look like.
            no_crypto_manager.create_checkpoint(path)
            original_content[path] = no_crypto_manager.get(path)['content']

        updated_content = {}
        for path in paths:
            # Create a new version of each notebook with a cell appended.
            model = no_crypto_manager.get(path=path)
            model['content'].cells.append(
                new_markdown_cell('Created by test: ' + path)
            )
            no_crypto_manager.save(model, path=path)

            # Store the updated content.
            updated_content[path] = no_crypto_manager.get(path)['content']

            # Create a checkpoint of the new content.
            no_crypto_manager.create_checkpoint(path)

        def check_path_content(path, mgr, expected):
            retrieved = mgr.get(path)['content']
            self.assertEqual(retrieved, expected[path])

        def check_reencryption(old, new):
            for path in paths:
                # We should no longer be able to retrieve notebooks from the
                # no-crypto manager.
                with assertRaisesHTTPError(self, 500):
                    old.get(path)

                # The new manager should read the latest version of each file.
                check_path_content(path, new, updated_content)

                # We should have two checkpoints available, one from the
                # original version of the file, and one for the updated
                # version.
                (new_cp, old_cp) = new.list_checkpoints(path)
                self.assertGreater(
                    new_cp['last_modified'],
                    old_cp['last_modified'],
                )

                # The old checkpoint should restore us to the original state.
                new.restore_checkpoint(old_cp['id'], path)
                check_path_content(path, new, original_content)

                # The new checkpoint should put us back into our updated state.
                # state.
                new.restore_checkpoint(new_cp['id'], path)
                check_path_content(path, new, updated_content)

        engine = create_engine(db_url)
        logger = Logger('Reencryption Testing')

        no_crypto_factory = {user_id: no_crypto}.__getitem__
        crypto1_factory = {user_id: crypto1}.__getitem__
        crypto2_factory = {user_id: crypto2}.__getitem__

        # Verify that reencryption is idempotent:
        for _ in range(2):
            reencrypt_all_users(
                engine,
                no_crypto_factory,
                crypto1_factory,
                logger,
            )
            check_reencryption(no_crypto_manager, manager1)

        for _ in range(2):
            reencrypt_all_users(
                engine,
                crypto1_factory,
                crypto2_factory,
                logger,
            )
            check_reencryption(manager1, manager2)

        with self.assertRaises(ValueError):
            # Using reencrypt_all_users with a no-encryption target isn't
            # supported.
            reencrypt_all_users(
                engine,
                crypto2_factory,
                no_crypto_factory,
                logger,
            )
        # There should have been no changes from the failed attempt.
        check_reencryption(manager1, manager2)

        # Unencrypt and verify that we can now read everything with the no
        # crypto manager.
        unencrypt_all_users(engine, crypto2_factory, logger)
        check_reencryption(manager2, no_crypto_manager)


class TestGenerateNotebooks(TestCase):

    def setUp(self):
        remigrate_test_schema()
        self.db_url = TEST_DB_URL
        self.engine = create_engine(self.db_url)
        encryption_pw = u'foobar'
        self.crypto_factory = single_password_crypto_factory(encryption_pw)

    def tearDown(self):
        clear_test_db()

    def populate_users(self, user_ids):
        """
        Create a `PostgresContentsManager` and notebooks for each user.
        """
        def encrypted_pgmanager(user_id):
            return PostgresContentsManager(
                user_id=user_id,
                db_url=self.db_url,
                crypto=self.crypto_factory(user_id),
                create_user_on_startup=True,
            )
        managers = {user_id: encrypted_pgmanager(user_id)
                    for user_id in user_ids}
        paths = {user_id: populate(managers[user_id]) for user_id in user_ids}
        return (managers, paths)

    def test_generate_files(self):
        """
        Create files for three users; try fetching them using `generate_files`.
        """
        user_ids = ['test_generate_files0',
                    'test_generate_files1',
                    'test_generate_files2']
        (managers, paths) = self.populate_users(user_ids)

        def get_file_dt(user_id, idx):
            path = paths[user_id][idx]
            return managers[user_id].get(path, content=False)['last_modified']

        # Find a split datetime midway through each user's list of files
        split_idx = len(paths[user_ids[0]]) // 2
        split_dts = [get_file_dt(user_id, split_idx) for user_id in user_ids]

        def check_call(kwargs, expect_files_by_user):
            """
            Call `generate_files`; check that all expected files are found,
            with the correct content.
            """
            file_record = {user_id: [] for user_id in expect_files_by_user}
            for result in generate_files(self.engine, self.crypto_factory,
                                         **kwargs):
                manager = managers[result['user_id']]

                # This recreates functionality from
                # `manager._notebook_model_from_db` to match with the model
                # returned by `manager.get`.
                nb = result['content']
                manager.mark_trusted_cells(nb, result['path'])

                # Check that the content returned by the pgcontents manager
                # matches that returned by `generate_files`
                self.assertEqual(nb, manager.get(result['path'])['content'])

                file_record[result['user_id']].append(result['path'])

            # Make sure all files were found
            for user_id in expect_files_by_user:
                self.assertEqual(sorted(file_record[user_id]),
                                 sorted(expect_files_by_user[user_id]))

        # Expect all files given no `min_dt`/`max_dt`
        check_call({}, paths)

        # `min_dt` is in the middle of 1's files; we get the latter half of 1's
        # and all of 2's
        check_call({'min_dt': split_dts[1]},
                   {
                       user_ids[0]: [],
                       user_ids[1]: paths[user_ids[1]][split_idx:],
                       user_ids[2]: paths[user_ids[2]],
                   })

        # `max_dt` is in the middle of 1's files; we get all of 0's and the
        # beginning half of 1's
        check_call({'max_dt': split_dts[1]},
                   {
                       user_ids[0]: paths[user_ids[0]],
                       user_ids[1]: paths[user_ids[1]][:split_idx],
                       user_ids[2]: [],
                   })

        # `min_dt` is in the middle of 0's files cutting off 0's beginning half
        # `max_dt` is in the middle of 2's files cutting off 2's latter half
        check_call({'min_dt': split_dts[0], 'max_dt': split_dts[2]},
                   {
                       user_ids[0]: paths[user_ids[0]][split_idx:],
                       user_ids[1]: paths[user_ids[1]],
                       user_ids[2]: paths[user_ids[2]][:split_idx],
                   })

    def test_generate_checkpoints(self):
        """
        Create checkpoints in three stages; try fetching them with
        `generate_checkpoints`.
        """
        user_ids = ['test_generate_checkpoints0',
                    'test_generate_checkpoints1',
                    'test_generate_checkpoints2']
        (managers, paths) = self.populate_users(user_ids)

        def update_content(user_id, path, text):
            """
            Add a Markdown cell and save the notebook.

            Returns the new notebook content.
            """
            manager = managers[user_id]
            model = manager.get(path)
            model['content'].cells.append(
                new_markdown_cell(text + ' on path: ' + path)
            )
            manager.save(model, path)
            return manager.get(path)['content']

        # Each of the next three steps creates a checkpoint for each notebook
        # and stores the notebook content in a dict, keyed by the user id,
        # the path, and the datetime of the new checkpoint.

        # Begin by making a checkpoint for the original notebook content.
        beginning_content = {}
        for user_id in user_ids:
            for path in paths[user_id]:
                content = managers[user_id].get(path)['content']
                dt = managers[user_id].create_checkpoint(path)['last_modified']
                beginning_content[user_id, path, dt] = content

        # Update each notebook and make a new checkpoint.
        middle_content = {}
        middle_min_dt = None
        for user_id in user_ids:
            for path in paths[user_id]:
                content = update_content(user_id, path, '1st addition')
                dt = managers[user_id].create_checkpoint(path)['last_modified']
                middle_content[user_id, path, dt] = content
                if middle_min_dt is None:
                    middle_min_dt = dt

        # Update each notebook again and make another checkpoint.
        end_content = {}
        end_min_dt = None
        for user_id in user_ids:
            for path in paths[user_id]:
                content = update_content(user_id, path, '2nd addition')
                dt = managers[user_id].create_checkpoint(path)['last_modified']
                end_content[user_id, path, dt] = content
                if end_min_dt is None:
                    end_min_dt = dt

        def merge_dicts(*args):
            result = {}
            for d in args:
                result.update(d)
            return result

        def check_call(kwargs, expect_checkpoints_content):
            """
            Call `generate_checkpoints`; check that all expected checkpoints
            are found, with the correct content.
            """
            expect_checkpoints = expect_checkpoints_content.keys()
            checkpoint_record = []
            for result in generate_checkpoints(self.engine,
                                               self.crypto_factory, **kwargs):
                manager = managers[result['user_id']]

                # This recreates functionality from
                # `manager._notebook_model_from_db` to match with the model
                # returned by `manager.get`.
                nb = result['content']
                manager.mark_trusted_cells(nb, result['path'])

                # Check that the checkpoint content matches what's expected
                key = (result['user_id'], result['path'],
                       result['last_modified'])
                self.assertEqual(nb, expect_checkpoints_content[key])

                checkpoint_record.append(key)

            # Make sure all checkpoints were found
            self.assertEqual(sorted(checkpoint_record),
                             sorted(expect_checkpoints))

        # No `min_dt`/`max_dt`
        check_call({}, merge_dicts(beginning_content,
                                   middle_content, end_content))

        # `min_dt` cuts off `beginning_content` checkpoints
        check_call({'min_dt': middle_min_dt},
                   merge_dicts(middle_content, end_content))

        # `max_dt` cuts off `end_content` checkpoints
        check_call({'max_dt': end_min_dt},
                   merge_dicts(beginning_content, middle_content))

        # `min_dt` and `max_dt` together isolate `middle_content`
        check_call({'min_dt': middle_min_dt, 'max_dt': end_min_dt},
                   middle_content)

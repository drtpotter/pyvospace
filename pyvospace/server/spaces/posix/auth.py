#
#    ICRAR - International Centre for Radio Astronomy Research
#    (c) UWA - The University of Western Australia, 2018
#    Copyright by UWA (in the framework of the ICRAR)
#    All rights reserved
#
#    This library is free software; you can redistribute it and/or
#    modify it under the terms of the GNU Lesser General Public
#    License as published by the Free Software Foundation; either
#    version 2.1 of the License, or (at your option) any later version.
#
#    This library is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#    Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with this library; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston,
#    MA 02111-1307  USA

import os
import json

from aiohttp import helpers, web
from aiohttp_security.abc import AbstractAuthorizationPolicy
from aiohttp_security import remember, forget
from passlib.hash import pbkdf2_sha256

from pyvospace.core.model import PushToSpace, Property
from .utils import statvfs, lstat


PROTECTED_URI = [#'ivo://ivoa.net/vospace/core#title',
                 'ivo://ivoa.net/vospace/core#creator',
                 #'ivo://ivoa.net/vospace/core#subject',
                 #'ivo://ivoa.net/vospace/core#description',
                 #'ivo://ivoa.net/vospace/core#publisher',
                 #'ivo://ivoa.net/vospace/core#contributor',
                 #'ivo://ivoa.net/vospace/core#date',
                 'ivo://ivoa.net/vospace/core#type',
                 'ivo://ivoa.net/vospace/core#format',
                 'ivo://ivoa.net/vospace/core#identifier',
                 'ivo://ivoa.net/vospace/core#source',
                 'ivo://ivoa.net/vospace/core#language',
                 'ivo://ivoa.net/vospace/core#relation',
                 'ivo://ivoa.net/vospace/core#coverage',
                 'ivo://ivoa.net/vospace/core#rights',
                 'ivo://ivoa.net/vospace/core#availableSpace',
                 'ivo://ivoa.net/vospace/core#groupread',
                 'ivo://ivoa.net/vospace/core#groupwrite',
                 'ivo://ivoa.net/vospace/core#publicread',
                 'ivo://ivoa.net/vospace/core#quota',
                 'ivo://ivoa.net/vospace/core#length',
                 'ivo://ivoa.net/vospace/core#mtime',
                 'ivo://ivoa.net/vospace/core#ctime',
                 'ivo://ivoa.net/vospace/core#btime']


class DBUserNodeAuthorizationPolicy(AbstractAuthorizationPolicy):

    def __init__(self, space_name, db_pool, root_dir):
        super().__init__()
        self.space_name = space_name
        self.db_pool = db_pool
        self.root_dir = root_dir

    def _any_value_in_lists(self, a, b):
        return any(i in a for i in b)

    def _any_property_in_protected(self, a):
        return any(i.uri in PROTECTED_URI for i in a)

    async def authorized_userid(self, identity):
        async with self.db_pool.acquire() as conn:
            results = await conn.fetchrow("select * from users "
                                          "where username=$1 and space_name=$2",
                                          identity, self.space_name)
        if not results:
            return None
        return results['username']

    async def permits(self, identity, permission, context=None):
        async with self.db_pool.acquire() as conn:
            user = await conn.fetchrow("select * from users "
                                       "where username=$1 and space_name=$2",
                                       identity, self.space_name)
            if not user:
                raise web.HTTPForbidden(f"{identity} not found.")

        if permission == 'createNode':
            parent = context[0]
            node = context[1]
            modify_properties = self._any_property_in_protected(node.properties.values())
            # User trying to create a protected property
            if modify_properties is True:
                return False
            # allow root node creation
            if parent.path == '/' and user['admin']:
                return True
            else:
                # check if the parent container is owned by the user
                if parent.owner == identity:
                    return True
                return self._any_value_in_lists(parent.group_write, user['groupwrite'])

        elif permission == 'setNode':
            node = context
            modify_properties = self._any_property_in_protected(context.properties.values())
            # User trying to update a protected property
            if modify_properties is True:
                return False
            if node.owner == identity:
                return True
            return self._any_value_in_lists(node.group_write, user['groupwrite'])

        elif permission == 'getNode':
            node = context
            real_path = os.path.normpath(f"{self.root_dir}/{node.path}")

            struct_lstat = await lstat(real_path)
            struct_statvfs = await statvfs(real_path)
            struct_statvfs_dict = dict((key, getattr(struct_statvfs, key)) for key in ('f_bavail', 'f_bfree',
                                                                                       'f_blocks', 'f_bsize',
                                                                                       'f_favail', 'f_ffree',
                                                                                       'f_files', 'f_flag',
                                                                                       'f_frsize', 'f_namemax'))

            prop_length = Property('ivo://ivoa.net/vospace/core#length', struct_lstat.st_size)
            prop_btime = Property('ivo://ivoa.net/vospace/core#btime', struct_lstat.st_mtime)
            prop_ctime = Property('ivo://ivoa.net/vospace/core#ctime', struct_lstat.st_ctime)
            prop_mtime = Property('ivo://ivoa.net/vospace/core#mtime', struct_lstat.st_mtime)
            prop_statfs = Property('ivo://icrar.org/vospace/core#statfs', json.dumps(struct_statvfs_dict))
            node.add_property(prop_length)
            node.add_property(prop_btime)
            node.add_property(prop_ctime)
            node.add_property(prop_mtime)
            node.add_property(prop_statfs)
            return True

        elif permission in ('moveNode', 'copyNode'):
            src = context[0]
            dest = context[1]
            if dest.path == '/' and user['admin']:
                return True
            if src.owner == identity and dest.owner == identity:
                return True
            if self._any_value_in_lists(src.group_write, user['groupwrite']) and \
                    self._any_value_in_lists(dest.group_write, user['groupwrite']):
                return True
            return False

        elif permission == 'createTransfer':
            return True

        elif permission == 'deleteNode':
            node = context
            if node.owner == identity:
                return True
            return self._any_value_in_lists(node.group_write, user['groupwrite'])

        elif permission == 'dataTransfer':
            job = context
            if job.transfer.target.owner == identity:
                return True
            if isinstance(job.transfer, PushToSpace):
                return self._any_value_in_lists(job.transfer.target.group_write, user['groupwrite'])
            else:
                return self._any_value_in_lists(job.transfer.target.group_read, user['groupread']) or \
                       self._any_value_in_lists(job.transfer.target.group_write, user['groupwrite'])

        elif permission in ('runJob', 'abortJob'):
            job = context
            if job.owner == identity:
                return True

        return False


class DBUserAuthentication(object):
    def __init__(self, space_name, db_pool):
        self.db_pool = db_pool
        self.space_name = space_name

    async def get_user(self, identity):
        async with self.db_pool.acquire() as conn:
            return await conn.fetchrow("select * from users where username=$1 and space_name=$2",
                                       identity, self.space_name)

    async def check_credentials(self, username, password):
        user = await self.get_user(username)
        if not user:
            return None

        if pbkdf2_sha256.verify(password, user['password']):
            return username
        return None

    async def login(self, request):
        try:
            auth = helpers.BasicAuth.decode(request.headers['Authorization'])
        except:
            return web.HTTPForbidden()
        try:
            user = await self.check_credentials(auth.login, auth.password)
            if not user:
                return web.HTTPForbidden()

            response = web.Response(status=200)
            await remember(request, response, user)
            return response
        except web.HTTPForbidden:
            raise
        except Exception:
            raise web.HTTPInternalServerError()

    async def logout(self, request):
        try:
            response = web.Response()
            await forget(request, response)
            return response
        except Exception:
            raise web.HTTPInternalServerError()

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
import io
from aiofiles.os import stat
import asyncio
import aiohttp
import aiofiles
import datetime
import xml.etree.ElementTree as ElementTree

from aiohttp import web
from pyvospace.server import fuzz

class CountedReader:
    """A wrapper class to count the number of bytes being sent from a stream"""
    def __init__(self, content):
        self._content=content
        self._size=0
        self._iter=None

    def __aiter__(self):
        #self._iter=self._content.__aiter__()
        self._iter=self._content.iter_chunked(io.DEFAULT_BUFFER_SIZE)
        return self

    async def __anext__(self):
        buffer=await self._iter.__anext__()
        self._size+=len(buffer)
        return buffer

class ControlledReader:
    """A wrapper class to limit the number of bytes returned from a stream
    to exactly content_length bytes"""

    def __init__(self, content, content_length):
        self._content = content
        self._content_length=content_length
        self._bytes_read = 0
        self._iter = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        # What is the minimum number of bytes to read?
        bytes_to_read = min(io.DEFAULT_BUFFER_SIZE, self._content_length - self._bytes_read)
        if bytes_to_read <= 0:
            raise StopAsyncIteration
        else:
            buffer = await self._content.readexactly(bytes_to_read)
            self._bytes_read+=bytes_to_read
            return buffer

def convert_to_epoch_seconds(date):
    # Convert a specific date string or date object to a number of seconds since
    # the UNIX epoch.
    if isinstance(date, str):
        dt = datetime.datetime.strptime(date, "%Y-%m-%dT%H:%M:%S.%f")
    elif isinstance(date, datetime.date):
        dt=date
    else:
        return None

    # Get the number of seconds since the UNIX epoch.
    seconds=str(int((dt - datetime.datetime(1970, 1, 1)).total_seconds()))
    return seconds

async def recv_file_from_ngas(session, hostname, port, filename_ngas, filename_local):

    """Get a single file from NGAS and put it into filename_local"""

    # The URL to contact the NGAS server
    url = f'http://{hostname}:{port}/RETRIEVE'

    # Make up the filename for retrieval from NGAS
    # How can I get the uuid from the database?
    params = {"file_id": filename_ngas}

    # Connect to NGAS
    resp_ngas = await session.get(url, params=params)

    # Rudimentry error checking on the NGAS connection
    if resp_ngas.status != 200:
        raise aiohttp.web.HTTPServerError(reason="Error in connecting to NGAS server")

    # Open the file for writing
    async with aiofiles.open(filename_local, 'wb') as fd:
        # Connect to the NGAS server and download the file
        async for chunk in resp_ngas.content.iter_chunked(io.DEFAULT_BUFFER_SIZE):
            if chunk:
                await fd.write(chunk)


async def send_file_to_ngas(session, hostname, port, filename_ngas, filename_local):

    #pdb.set_trace()

    """Send a single file to an NGAS server"""
    try:

        # Create parameters for the upload
        params = {"filename": filename_ngas,
                  "file_id" : filename_ngas,
                  "mime_type": "application/octet-stream"}

        # The URL to contact the NGAS server
        url=f'http://{hostname}:{port}/ARCHIVE'

        # Make sure a the file exists
        if filename_local is None or not os.path.isfile(filename_local):
            raise FileNotFoundError

        # Get the size of the file for content-length
        file_size = (await stat(filename_local)).st_size

        if file_size==0:
            raise ValueError(f"file {filename_local} has 0 size")

        async with aiofiles.open(filename_local, 'rb') as fd:
            # Connect to the NGAS server and upload the file
            resp = await session.post(url, params=params,
                                    data=fd,
                                    headers={"content-length" : str(file_size)})

            if resp.status!=200:
                raise aiohttp.ServerConnectionError("Error received in connecting to NGAS server")

        return(file_size)

    except Exception as e:
        # Do we do anything here?
        raise e

async def send_stream_to_ngas(request: aiohttp.web.Request, session, hostname, port, filename_ngas, logger):

    """If an incoming POST request has the content-length, send a stream direct to NGAS"""
    try:

        # Create parameters for the upload
        params = {"filename": filename_ngas,
                  "file_id" : filename_ngas,
                  "mime_type": "application/octet-stream"}

        # The URL to contact the NGAS server
        url="http://"+str(hostname)+":"+str(port)+"/ARCHIVE"

        # Test for content-length
        if 'content-length' not in request.headers:
            raise aiohttp.ServerConnectionError("No content-length in header")

        content_length=int(request.headers['Content-Length'])

        if content_length==0:
            raise ValueError

        # Create a ControlledReader from the content
        reader=ControlledReader(request.content, content_length)

        # Test for proper implementation
        if 'transfer-encoding' in request.headers:
            if request.headers['transfer-encoding']=="chunked":
                raise aiohttp.ServerConnectionError("Error, content length defined but transfer-encoding is chunked")

        # Connect to the NGAS server and upload
        resp = await session.post(url, params=params,
                                    data=reader,
                                    headers={"content-length" : str(content_length)})

        # Handle the response in a specific way, as requested
        if resp.status==200:
            # Dig into the XML response for output, we are looking for SUCCESS
            feedback = await resp.text()
            xmltree = ElementTree.fromstring(feedback)

            # Create a dictionary of all XML elements in the response
            elements = {t.tag: t for t in xmltree.iter()}
            # Status of the NGAS transaction
            status=elements["Status"].get("Status")

            if status=="SUCCESS":
                return(content_length)
            else:
                raise aiohttp.ServerConnectionError("Error received in connecting to NGAS server")
                return(None)
        else:
            raise aiohttp.ServerConnectionError("Error received in connecting to NGAS server")
            return(None)

    except Exception as e:
        # Do we do anything here?
        raise e


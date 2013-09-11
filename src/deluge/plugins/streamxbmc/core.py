# -*- coding: utf-8 -*-

#
# core.py
#
# Copyright (C) 2013 hal9000 <hal9000@example.com>
#
# Basic plugin template created by:
# Copyright (C) 2008 Martijn Voncken <mvoncken@gmail.com>
# Copyright (C) 2007-2009 Andrew Resch <andrewresch@gmail.com>
# Copyright (C) 2009 Damien Churchill <damoxc@gmail.com>
# Copyright (C) 2010 Pedro Algarvio <pedro@algarvio.me>
#
# Deluge is free software.
#
# You may redistribute it and/or modify it under the terms of the
# GNU General Public License, as published by the Free Software
# Foundation; either version 3 of the License, or (at your option)
# any later version.
#
# deluge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with deluge.    If not, write to:
# 	The Free Software Foundation, Inc.,
# 	51 Franklin Street, Fifth Floor
# 	Boston, MA  02110-1301, USA.
#
#    In addition, as a special exception, the copyright holders give
#    permission to link the code of portions of this program with the OpenSSL
#    library.
#    You must obey the GNU General Public License in all respects for all of
#    the code used other than OpenSSL. If you modify file(s) with this
#    exception, you may extend this exception to your version of the file(s),
#    but you are not obligated to do so. If you do not wish to do so, delete
#    this exception statement from your version. If you delete this exception
#    statement from all source files in the program, then also delete it here.
#


import logging
import re
import cgi
import base64
import json
import time
import os.path


import deluge.configmanager
from deluge.plugins.pluginbase import CorePluginBase
import deluge.component as component
from deluge.core.rpcserver import export
from deluge._libtorrent import lt as libtorrent


from twisted.web import server, resource
from twisted.internet import reactor, task


DEFAULT_PREFS = {
    'host': '127.0.0.1',
    'port': 5757,
    'storage': 100
}


log = logging.getLogger(__name__)



class State:
    torrents = component.get('Core').torrentmanager.torrents

    def __init__(self, ip, get_all_state):
        self.ip = ip
        self.get_all_state = get_all_state
        self.flush()

    def flush(self):
        self.state = None
        self.tid = None
        self.fid = None

        self.peers = 0
        self.seeds = 0
        self.down_speed = 0
        self.up_speed = 0

        self.filename = None
        self.download = 0
        self.size = 0
        self.buffer = 0
        self.fbuffer = 0

        self.total_download = 0
        self.total_upload = 0
        self.total_size = 0
        self.total_buffer = 0

        # private
        self.storage = 0
        self.buffer_min = 0
        self.buffer_percent = 0

        self.first = 0
        self.first_length = 0
        self.last = 0
        self.piece_length = 0
        self.num_pieces = 0
        self.num_high_pieces = 0
        self.high_pieces = {}



    def add(self, tid, fid, storage, buffer_min, buffer_percent):
        if self.tid != tid or self.fid != fid:
            if self.state == 'down':
                self.resume()
            self.flush()
            self.state = 'init'
            self.tid = tid
            self.fid = fid
            self.storage = storage
            self.buffer_min = buffer_min
            self.buffer_percent = buffer_percent


    def clear_storage(self):
        length = deluge.configmanager.ConfigManager("streamxbmc.conf", DEFAULT_PREFS)['storage']*1024*1024*1024 # GB

        data = deluge.configmanager.ConfigManager("streamxbmcstorage.conf", dict(storage=[], lock=[]))
        
        lock = [x for x in data['lock'] if x['ip'] != self.ip and int(time.time()) - x['time'] < 604800] # 1 week
        lock.append({'ip': self.ip, 'tid': self.tid, 'time': int(time.time()), 'size': self.total_size})

        lock_tid = dict([(x['tid'], x['size']) for x in lock])
        lock_size = sum(lock_tid.values())

        storage = [x for x in data['storage'] if x['tid'] in self.torrents and x['tid'] not in lock_tid]
        storage.sort(cmp=lambda t1, t2: cmp(t1['time'], t2['time']))
        storage.reverse()

        core = component.get('Core')
        bad = []

        while True:
            if not storage or sum([x['size'] for x in storage]) + lock_size <= length:
                break
            torrent = storage.pop(0)
            try:
                r = core.torrentmanager.remove(torrent['tid'], True)
            except InvalidTorrentError:
                bad.append(torrent)
            else:
                if not r:
                    bad.append(torrent)

        storage.append({'time': int(time.time()), 'tid': self.tid, 'size': self.total_size})
        if bad:
            storage.extend(bad)

        data['storage'] = storage
        data['lock'] = lock

        log.debug('StreamXBMC: lock updated: ' + str(data['lock']))
        log.debug('StreamXBMC: storage updated: ' + str(data['storage']))

        data.save()
        
        

    def get_state(self):
        res = dict(
            state = self.state or 'stop',
            peers = self.peers,
            seeds = self.seeds,
            dspeed = self.down_speed,
            uspeed = self.up_speed,

            filename = self.filename,
            download = self.download,
            size = self.size,
            buffer = self.buffer,
            fbuffer = self.fbuffer,

            tdownload = self.total_download,
            tupload = self.total_upload,
            tsize = self.total_size,
            tbuffer = self.total_buffer
        )

        return res


    def loop(self):
        state = self.state
        self.update()
        if state != self.state and state == 'down':
            self.resume()
            

    def resume(self):
        if self.state:
            self.torrents[self.tid].handle.set_sequential_download(False)
            self.torrents[self.tid].handle.prioritize_pieces([1]*self.num_pieces)
        all_state = self.get_all_state()
        for tid in self.torrents.keys():
            if tid not in all_state or all_state[tid] not in ('init', 'down') or self.tid == tid:
                self.torrents[tid].resume()



    def update(self):
        if not self.state:
            return

        if self.tid not in self.torrents:
            if self.state != 'init':
                self.flush()
            return

        torrent = self.torrents[self.tid]

        if not torrent.handle.has_metadata():
            return

        if not self.filename:
            files = [x for x in torrent.get_files() if self.fid == x['index']]
            if not files:
                return

            self.filename = os.path.join(torrent.options['download_location'], files[0]['path'])
            self.size = files[0]['size']
            self.total_size = torrent.torrent_info.total_size()
            self.num_pieces = torrent.torrent_info.num_pieces()
            self.piece_length = torrent.torrent_info.piece_length()

            first = torrent.torrent_info.map_file(self.fid, 0, 0)
            self.first = first.piece
            self.first_length = torrent.torrent_info.piece_size(first.piece) - first.start
            
            self.last  = torrent.torrent_info.map_file(self.fid, max(files[0]['size'] - 1, 0), 0).piece + 1

            self.total_buffer = self.buffer_percent*self.size/100
            if self.total_buffer < self.buffer_min:
                self.total_buffer = self.buffer_min
            if self.total_buffer > self.size:
                self.total_buffer = self.size

            self.num_high_pieces = int(self.total_buffer/self.piece_length) + 1

            self.fbuffer_piece = torrent.torrent_info.map_file(self.fid, self.total_buffer - 1, 0).piece + 1

            self.clear_storage()


        status = torrent.status

        self.peers = status.num_peers
        self.seeds = status.num_seeds
        self.down_speed = status.download_payload_rate
        self.download = self.buffer = torrent.handle.file_progress()[self.fid]
        self.total_download = status.total_payload_download
        #self.total_download = status.total_done
        self.total_upload = status.total_payload_upload

        #if status.state == status.seeding or status.state == status.finished:
        #    self.state = 'seed'
        #    return

        if torrent.handle.is_seed():
            self.state = 'seed'
            return

        pieces = status.pieces
        if not pieces:
            return

        file_pieces = pieces[self.first:self.last]

        if not [x for x in file_pieces if not x]:
            self.state = 'up'
            return

        else:

            if self.state != 'down':
                torrent.handle.prioritize_pieces([0]*self.num_pieces)
                torrent.handle.set_sequential_download(True)
                all_state = self.get_all_state()
                for tid in [x for x in self.torrents.keys() if x != self.tid and (x not in all_state or all_state[x] not in ('init', 'down'))]:
                    self.torrents[tid].pause()

            self.fbuffer = min(self.total_buffer, max(0, self.piece_length*len([x for x in file_pieces[0:self.fbuffer_piece] if x])))

            self.prioritize_up(file_pieces)
            self.state = 'down'


    def prioritize_up(self, file_pieces):
        count = 0
        self.buffer = 0
        for i, piece in enumerate(file_pieces):
            if not piece:

                count += 1
                if count > self.num_high_pieces:
                    break

                index = self.first + i
                if index not in self.high_pieces:
                    self.high_pieces[index] = True
                    self.torrents[self.tid].handle.piece_priority(index, 7)

            elif count == 0:
                if i == 0:
                    self.buffer = self.first_length
                else:
                    self.buffer += self.piece_length

        if self.buffer > self.size:
            self.buffer = self.size


    def read_file(self, offset):
        if not self.filename or self.tid not in self.torrents or offset >= self.size:
            return

        pieces = self.torrents[self.tid].status.pieces
        if not pieces:
            return

        map_file = self.torrents[self.tid].torrent_info.map_file(self.fid, offset, self.piece_length)
        log.debug('StreamXBMC: map_file: piece=%r, start=%r, length=%r' % (map_file.piece, map_file.start, map_file.length))
        
        if not pieces[map_file.piece]:
            return

        piece_size = self.torrents[self.tid].torrent_info.piece_size(map_file.piece)
        length = min(piece_size - map_file.start, map_file.length)
        log.debug('StreamXBMC: piece_size: size=%r, length=%r' % (piece_size, length))

        try:
            f = open(self.filename, 'rb')
        except:
            log.error('StreamXBMC: error open file: %r' % self.filename)
            return
        else:
            log.debug('StreamXBMC: open file: %r' % self.filename)
            try:
                f.seek(offset, 0)
            except:
                f.close()
                log.error('StreamXBMC: error seek: offset=%r, file=%r' % (offset, self.filename))
                return
            else:
                log.debug('StreamXBMC: seek: cursor=%r, file=%r' % (offset, self.filename))
                try:
                    body = f.read(length)
                except:
                    f.close()
                    log.error('StreamXBMC: error read: length=%r, file=%r' % (length, self.filename))
                    return
                else:
                    f.close()
                    log.debug('StreamXBMC: read: length=%r, file=%r' % (length, self.filename))
                    return body





class StateList:
    state = {}

    def add(self, ip, tid, fid, storage, buffer_min, buffer_percent):
        if ip not in self.state:
            self.state[ip] = State(ip, self.get_all_state)
        self.state[ip].add(tid, fid, storage, buffer_min, buffer_percent)

    def get_state(self, ip):
        if ip not in self.state:
            self.state[ip] = State(ip, self.get_all_state)
        return self.state[ip].get_state()

    def download(self, ip, offset):
        if ip not in self.state:
            self.state[ip] = State(ip, self.get_all_state)
        return self.state[ip].read_file(offset)

    def get_torrents(self):
        return []

    def loop(self):
        ips = self.state.keys()
        for ip in ips:
            self.state[ip].loop()

    def get_all_state(self):
        return dict([(x.tid, x.state) for x in self.state.values() if x.tid])



STATE = StateList()



class HTTP(resource.Resource):
    isLeaf = True
    re_get = re.compile('^/((?:state)|(?:download)|(?:list))')
    re_post = re.compile('^/add')

    def render_GET(self, request):
        r = self.re_get.search(request.path)
        if not r:
            return self.bad_request(request)

        ip = request.getClientIP() or '0.0.0.0'

        log.debug('StreamXBMC: request: %r, %r' % (request, ip))

        api = r.group(1)

        if api == 'download':
            offset = int(request.args['offset'][0]) if 'offset' in request.args and request.args['offset'][0].isdigit() and int(request.args['offset'][0]) >= 0 else 0
            data = STATE.download(ip, offset)
            if data is None:
                request.setResponseCode(404)
                return ''
            else:
                return data

        elif api == 'state':
            return json.dumps(STATE.get_state(ip))

        else:
            return json.dumps(STATE.get_torrents())
            

    def render_POST(self, request):
        log.debug('StreamXBMC: post: %r' % request.path)

        r = self.re_post.search(request.path)
        if not r:
            return self.bad_request(request)
        log.debug('StreamXBMC: request: %r' % request)

        headers = request.getAllHeaders()
        try:
            files = cgi.FieldStorage(
                fp=request.content,
                headers = headers,
                environ = {'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': headers['content-type']}
            )
            filename, torrent = files['torrent_file'].filename, files['torrent_file'].value
            torrent_info = libtorrent.torrent_info(libtorrent.bdecode(torrent))
        except Exception, e:
            log.error('StreamXBMC: unable to parse torrent file: %s', e)
            return self.bad_request(request)
        else:
            if not torrent_info:
                log.error('StreamXBMC: error getting info_torrent')
                return self.bad_request(request)

            torrent_id = str(torrent_info.info_hash())
            log.debug('StreamXBMC: add torrent: get hash: torrent_id: %r' % torrent_id)

            count_files = len(torrent_info.files())
            if not count_files:
                log.error('StreamXBMC: add torrent: unknow count of files: %r' % count_files)
                return self.bad_request(request)
            log.debug('StreamXBMC: add torrent: count files: %r' % count_files)

            file_id = int(request.args['fid'][0]) if 'fid' in request.args and request.args['fid'][0].isdigit() and int(request.args['fid'][0]) >= 0 else 0
            if file_id >= count_files:
                log.error('StreamXBMC: add torrent: bad file_id: %r' % file_id)
                return self.bad_request(request)

            core = component.get("Core")
            if not core.add_torrent_file(filename, base64.encodestring(torrent), {}):
                if torrent_id not in core.torrentmanager.torrents:
                    log.error('StreamXBMC: error adding torrent: %r' % torrent_id)
                    return self.bad_request(request)

            storage = int(request.args['storage'][0]) if 'storage' in request.args and request.args['storage'][0].isdigit() and int(request.args['storage'][0]) >= 0 else 0
            buffer_min = int(request.args['buffer_min'][0]) if 'buffer_min' in request.args and request.args['buffer_min'][0].isdigit() and int(request.args['buffer_min'][0]) >= 0 else 20
            buffer_percent = int(request.args['buffer_percent'][0]) if 'buffer_percent' in request.args and request.args['buffer_percent'][0].isdigit() and int(request.args['buffer_percent'][0]) >= 0 and int(request.args['buffer_percent'][0]) <= 100 else 3

            STATE.add((request.getClientIP() or '0.0.0.0'), torrent_id, file_id, 1024*1024*1024*storage, 1024*1024*buffer_min, buffer_percent)

            log.debug('StreamXBMC: torrent added: hash: %r' % torrent_id)
            return json.dumps({'tid': torrent_id})


    def bad_request(self, request):
        log.error('StreamXBMC: bad request: %r' % request)
        request.setResponseCode(400)
        return 'Bad Request'




class Core(CorePluginBase):
    _update = None
    _web = None

    def enable(self):
        self.config = deluge.configmanager.ConfigManager("streamxbmc.conf", DEFAULT_PREFS)
        self._update = task.LoopingCall(self.state_loop)
        self._update.start(1)
        self._web = reactor.listenTCP(self.config['port'], server.Site(HTTP()), interface=self.config['host'])
        log.debug('StreamXBMC: enable: port: ' + str(self.config['port']))


    def disable(self):
        if self._update and self._update.running:
            self._update.stop()
            self._web.stopListening()
            log.debug('StreamXBMC: disabled')


    def update(self):
        pass

    def state_loop(self):
        STATE.loop()


    @export
    def set_config(self, config):
        for key in config.keys():
            self.config[key] = config[key]
        self.config.save()


    @export
    def get_config(self):
        return self.config.config

import asyncio
from asyncio.exceptions import CancelledError
import concurrent.futures
import enum
import hashlib
import itertools
import json
import keyfinder
import multiprocessing
import os
import shutil
import watchdog.observers
import time

from tune_manager import mediafile
from tune_manager.importer import convert, beatport
from tune_manager.utils import image, file
from tune_manager.utils.watchdog import AsyncHandler

# This list specifies file extensions that are directly supported for
# importing, without requiring any type of conversion.
VALID_FORMATS = [".mp3", ".aif", ".aiff"]


def future_raise(future):
    if future.exception():
        raise future.exception()


def file_id(path):
    """
    Compute the identifier of a file given it's path. This is simply the md5
    sum of the file path without the file extension.
    """
    return hashlib.md5(os.path.splitext(path)[0].encode("utf-8")).hexdigest()


def group_events(events):
    def key_on_type(k):
        return k["type"]

    events.sort(key=key_on_type)

    for event_type, items in itertools.groupby(events, key=key_on_type):
        items = [e["item"] for e in items]
        yield {"type": event_type, "items": items}


class EventType(enum.Enum):
    TRACK_DETAILS = enum.auto()
    TRACK_REMOVED = enum.auto()
    TRACK_PROCESSING = enum.auto()
    TRACK_UPDATE = enum.auto()
    TRACK_SAVED = enum.auto()


class TrackProcesses(enum.Enum):
    CONVERTING = enum.auto()
    KEY_COMPUTING = enum.auto()
    BEATPORT_IMPORT = enum.auto()


class TrackProcessor(object):
    """
    TrackProcessor provides a service for managing and processing the tracks
    currently in the importing collection.

    This service is resposible for:

    * Watching the filesystem for tracks to be removed, reporting track
      details, and triggering track processing upon these events.

    * Queueing tracks to be converted if they are not a valid format.

    * Queueing key detection for new tracks.

    The following message types will be sent:

    - TRACK_DETAILS

      When the client first connects to the endpoint all tracks that are
      currently being tracked in the new tracks directory will be reported as
      added. This directory will also be watched which will send new track
      details.

    - TRACK_REMOVED

      If a file is removed from the directory

    - TRACK_PROCESSING

      This event is triggered when the track is undergoing a processing event
      that may take enough time that it warrents representation in the UI.

      The following processing events are possible:

      * CONVERTING

        The track was found, but is not in a valid format and must first be
        converted.

      * KEY_COMPUTING

        Computation of musical key takes some time, this message will be
        reported to the client when a key for a track is beginning to be
        computed. When the client first connects.

      * BEATPORT_IMPORT

        Tracks purchased from beatport have identifying information which can
        be used to retrieve more information about the track.

    - TRACK_UPDATE

      Reported when partial information becomes available for a track. The
      event will report what processing event was fulfilled by this partial
      information with the 'completed_process' item key.

    The websocket connection is available at ws://localhost:9000.
    """

    def __init__(self, import_path, batch_period=300, loop=None):
        self.import_path = import_path
        self.batch_period = batch_period

        self.loop = loop or asyncio.get_event_loop()
        self.events = asyncio.Queue()
        self.connections = set()

        # Tracked mediafiles will be stored as a dict of their ID mapped to the
        # media file object representing them.
        self.mediafiles = {}

        # Track artwork as their md5 sums, an optimization
        self.artwork = {}

        # Current processing tracks
        self.processing = []

        # List of tracks that are expected to be removed and should not be
        # reported back as removed.
        self.expect_removed = []

        # The process executor will be used to parallelize key detection
        # Limit threads to the number of cores we have, key detection becomes a
        # pretty expensive computation.
        cores = multiprocessing.cpu_count()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=cores)

        # Setup filesystem watchdog
        watcher = AsyncHandler(self.loop, self.file_event)

        observer = watchdog.observers.Observer()
        observer.schedule(watcher, import_path, recursive=True)

        async def file_dispatcher():
            observer.start()
            while True:
                try:
                    await asyncio.sleep(1)
                except CancelledError:
                    break

        # kickoff coroutines
        self.loop.create_task(file_dispatcher())
        self.loop.create_task(self.dispatcher())

    async def open_connection(self, ws):
        self.connections.add(ws)
        self.report_state(ws)

        try:
            while True:
                try:
                    await ws.recv()
                except CancelledError:
                    break
        except Exception:
            self.connections.remove(ws)

    async def dispatcher(self):
        while True:
            try:
                # Batch messages together over this period of time
                await asyncio.sleep(self.batch_period / 1000)
                batch = []

                if self.events.empty():
                    continue

                while not self.events.empty():
                    batch.append(await self.events.get())

                # Nothing to dispatch without any connections
                if not self.connections:
                    continue

                for event in group_events(batch):
                    data = json.dumps(event)
                    for ws in self.connections:
                        asyncio.create_task(ws.send(data))
            except CancelledError:
                break

    async def file_event(self, event):
        """
        When the filesystem observer sees a new file added or removed from the
        import collection this method will be called, triggering the
        appropriate action.
        """

        commands = {"created": self.add_safe, "deleted": self.remove}

        if event.is_directory or event.event_type not in commands:
            return

        commands[event.event_type](event.src_path)

    def send_event(self, event_type, identifier, **kwargs):
        item = {"id": identifier}
        item.update(kwargs)

        self.events.put_nowait({"type": event_type.name, "item": item})

    def send_details(self, identifier, track):
        self.send_event(EventType.TRACK_DETAILS, identifier, **track)

    def send_processing(self, identifier, process):
        item = {"process": process.name}
        self.send_event(EventType.TRACK_PROCESSING, identifier, **item)

    def add_processing(self, identifier, process):
        self.processing.append((identifier, process))
        self.send_processing(identifier, process)

    def done_processing(self, identifier, completed_process, **kwargs):
        self.processing.remove((identifier, completed_process))

        item = {"completed_process": completed_process.name}
        item.update(kwargs)
        self.send_event(EventType.TRACK_UPDATE, identifier, **item)

    def report_state(self, ws):
        # Report existing tracks and current processes. Events will be batched
        # together and dispatched.
        for identifier, media in self.mediafiles.items():
            track = mediafile.serialize(media, trim_path=self.import_path)
            self.send_details(identifier, track)

        for identifier, process in self.processing:
            self.send_processing(identifier, process)

    def execute_paralell(self, fn, *args):
        self.executor.submit(fn, *args).add_done_callback(future_raise)

    def convert_track(self, identifier, path):
        process = TrackProcesses.CONVERTING
        self.add_processing(identifier, process)

        convert.convert_track(path)
        self.done_processing(identifier, process)

    def compute_key(self, identifier, media):
        process = TrackProcesses.KEY_COMPUTING
        self.add_processing(identifier, process)

        # Prefix key with leading zeros
        media.key = keyfinder.key(media.file_path).camelot().zfill(3)
        self.done_processing(identifier, process, key=media.key)

        media.save()

    def beatport_update(self, identifier, media):
        process = TrackProcesses.BEATPORT_IMPORT
        self.add_processing(identifier, process)

        fields = beatport.process(media)
        self.done_processing(identifier, process, **fields)

        media.save()

    def save_track(self, track, options={}):
        identifier = track["id"]
        assert identifier in self.mediafiles

        media = self.mediafiles[identifier]

        artwork = self.artwork.get(track["artwork"])
        if artwork:
            artwork = image.normalize_artwork(artwork)
            track["artwork"] = artwork
        else:
            track["artwork"] = None

        track = [(k, v) for k, v in track.items() if hasattr(media, k) and v]

        media.clear()
        for key, value in track:
            setattr(media, key, value)

        self.cache_art(media.artwork)
        media.save()

        standard_path = file.determine_path(media)
        migrate_path = options.get("migrate_path")
        if migrate_path:
            # Expect that this will be removed, do not notify the client, it
            # will handle removal of the track listing itself.
            self.expect_removed.append(identifier)

            path = os.path.join(migrate_path, standard_path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.move(media.file_path, path)

            media.file_path = path
            media.reload()

        # Link the track to it's other specified paths
        for link_path in options.get("link_paths", []):
            path = os.path.join(link_path, standard_path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.symlink(media.file_path, new_path)

        self.send_event(EventType.TRACK_SAVED, identifier)

    def save_all(self, tracks, options):
        for track in tracks:
            self.execute_paralell(self.save_track, track, options)

    def add_all(self):
        """
        Add all existing tracks in the import path
        """
        path = self.import_path
        types = tuple(VALID_FORMATS + convert.CONVERTABLE_FORMATS)

        for path in file.collect_files([path], recursive=True, types=types):
            self.add(path)

    def add_safe(self, path):
        self.add(path, safe_add=True)

    def add(self, path, safe_add=False):
        """
        Add a track to the tracked import list.
        """
        # Apple likes to litter these files into directories, don't even
        # attempt to read them as it will just immediately fail
        if os.path.basename(path).startswith("._"):
            return

        # Track ready to be reported
        self.execute_paralell(self.process_add, path, safe_add)

    def process_add(self, path, safe_add):
        identifier = file_id(path)
        ext = os.path.splitext(path)[1]

        # Ensure the file isn't still being written
        if safe_add:
            size = os.stat(path).st_size
            while True:
                time.sleep(0.5)
                latest_size = os.stat(path).st_size
                if latest_size == size:
                    break
                size = latest_size

        if ext == ".aiff":
            ext = ".aif"
            new_path = path[:-5] + ext
            os.rename(path, new_path)
            path = new_path
            identifier = file_id(path)

        # File may need to be transformed before it can be processed for
        # importing.
        if ext in convert.CONVERTABLE_FORMATS:
            self.execute_paralell(self.convert_track, identifier, path)
            return

        if ext not in VALID_FORMATS:
            return

        media = mediafile.MediaFile(path)

        # Recompute the key if it is missing or invalid
        valid_keys = keyfinder.notations.camelot.values()

        if not media.key or not media.key.strip("0") in valid_keys:
            media.key = ""
            self.execute_paralell(self.compute_key, identifier, media)

        # Request more details from beatport
        if beatport.has_metadata(media):
            self.execute_paralell(self.beatport_update, identifier, media)

        self.mediafiles[identifier] = media
        self.cache_art(media.artwork)

        # Report track details
        track = mediafile.serialize(media, trim_path=self.import_path)
        self.send_details(identifier, track)

    def remove(self, path):
        """
        Remove a track from the tracked import list.
        """
        identifier = file_id(path)

        if identifier not in self.mediafiles:
            return

        # TODO: This is somewhat wrong since some other tracks may be using
        # this artwork.
        for k in [a.key for a in self.mediafiles[identifier].artwork]:
            self.artwork.pop(k, None)

        del self.mediafiles[identifier]

        if identifier not in self.expect_removed:
            self.send_event(EventType.TRACK_REMOVED, identifier)
        else:
            self.expect_removed.remove(identifier)

    def cache_art(self, artwork):
        self.artwork.update({a.key: a for a in artwork})

from __future__ import absolute_import

import uuid
import logging

from symbolic import parse_addr, find_best_instruction, arch_get_ip_reg_name, \
    ObjectLookup
from symbolic.utils import make_buffered_slice_reader

from sentry import options
from sentry.cache import default_cache
from sentry.coreapi import cache_key_for_event
from sentry.plugins import Plugin2
from sentry.lang.native.minidump import get_attached_minidump, is_minidump_event, merge_symbolicator_minidump_response
from sentry.lang.native.unreal import is_unreal_event, reprocess_unreal_crash
from sentry.lang.native.symbolizer import Symbolizer, SymbolicationFailed
from sentry.lang.native.symbolicator import run_symbolicator, merge_symbolicator_image, create_minidump_task, handle_symbolicator_response_status
from sentry.lang.native.utils import get_sdk_from_event, handle_symbolication_failed, cpu_name_from_data, \
    merge_symbolicated_frame, rebase_addr, signal_from_data
from sentry.lang.native.systemsymbols import lookup_system_symbols
from sentry.models import Project
from sentry.utils import metrics
from sentry.utils.in_app import is_known_third_party
from sentry.utils.safe import get_path
from sentry.stacktraces.processing import StacktraceProcessor

logger = logging.getLogger(__name__)

FRAME_CACHE_VERSION = 6

SYMBOLICATOR_FRAME_ATTRS = ("instruction_addr", "package", "lang", "symbol",
                            "function", "symbol_addr", "filename", "lineno",
                            "line_addr")


def request_id_cache_key_for_event(data):
    return u'symbolicator:{1}:{0}'.format(data['project'], data['event_id'])


def minidump_reprocessed_cache_key_for_event(data):
    return u'symbolicator-minidump-processed:{1}:{0}'.format(data['project'], data['event_id'])


class NativeStacktraceProcessor(StacktraceProcessor):
    supported_platforms = ('cocoa', 'native')
    # TODO(ja): Clean up all uses of image type "apple", "uuid", "id" and "name"
    supported_images = ('apple', 'symbolic', 'elf', 'macho', 'pe')

    def __init__(self, *args, **kwargs):
        StacktraceProcessor.__init__(self, *args, **kwargs)

        self.arch = cpu_name_from_data(self.data)
        self.signal = signal_from_data(self.data)

        self.sym = None
        self.difs_referenced = set()

        images = get_path(self.data, 'debug_meta', 'images', default=(),
                          filter=self._is_valid_image)

        if images:
            self.available = True
            self.sdk_info = get_sdk_from_event(self.data)
            self.object_lookup = ObjectLookup(images)
            self.images = images
        else:
            self.available = False

    def _is_valid_image(self, image):
        # TODO(ja): Deprecate this. The symbolicator should take care of
        # filtering valid images.
        return bool(image) \
            and image.get('type') in self.supported_images \
            and image.get('image_addr') is not None \
            and image.get('image_size') is not None \
            and (image.get('debug_id') or image.get('id') or image.get('uuid')) is not None

    def close(self):
        StacktraceProcessor.close(self)
        if self.difs_referenced:
            metrics.incr(
                'dsyms.processed',
                amount=len(self.difs_referenced),
                skip_internal=True,
                tags={
                    'project_id': self.project.id,
                },
            )

    def find_best_instruction(self, processable_frame):
        """Given a frame, stacktrace info and frame index this returns the
        interpolated instruction address we then use for symbolication later.
        """
        if self.arch is None:
            return parse_addr(processable_frame['instruction_addr'])

        crashing_frame = False
        signal = None
        ip_reg = None

        # We only need to provide meta information for frame zero
        if processable_frame.idx == 0:
            # The signal is useful information for symbolic in some situations
            # to disambiugate the first frame.  If we can get this information
            # from the mechanism we want to pass it onwards.
            signal = self.signal

            registers = processable_frame.stacktrace_info.stacktrace.get('registers')
            if registers:
                ip_reg_name = arch_get_ip_reg_name(self.arch)
                if ip_reg_name:
                    ip_reg = registers.get(ip_reg_name)
            crashing_frame = True

        return find_best_instruction(
            processable_frame['instruction_addr'],
            arch=self.arch,
            crashing_frame=crashing_frame,
            signal=signal,
            ip_reg=ip_reg
        )

    def handles_frame(self, frame, stacktrace_info):
        if not self.available:
            return False

        platform = frame.get('platform') or self.data.get('platform')
        if platform not in self.supported_platforms:
            return False

        if frame.get('data', {}).get('symbolicator_status'):
            return False

        if 'instruction_addr' not in frame:
            return False

        return True

    def preprocess_frame(self, processable_frame):
        instr_addr = self.find_best_instruction(processable_frame)
        obj = self.object_lookup.find_object(instr_addr)

        processable_frame.data = {
            'instruction_addr': instr_addr,
            'obj': obj,
            'debug_id': obj.debug_id if obj is not None else None,
            'symbolserver_match': None,

            # `[]` is used to indicate to the symbolizer that the symbolicator
            # deliberately discarded this frame.
            # If the symbolicator did run and was not able to symbolize the
            # frame, this value will be a list with the raw frame as only item.
            'symbolicator_match': []
        }

        if obj is not None:
            processable_frame.set_cache_key_from_values(
                (
                    FRAME_CACHE_VERSION,
                    # Because the images can move around, we want to rebase
                    # the address for the cache key to be within the image
                    # the same way as we do it in the symbolizer.
                    rebase_addr(instr_addr, obj),
                    obj.debug_id,
                    obj.arch,
                    obj.size,
                )
            )

    def preprocess_step(self, processing_task):
        if not self.available:
            return False

        self.sym = Symbolizer()

        if options.get('symbolserver.enabled'):
            self.fetch_ios_system_symbols(processing_task)

        self.run_symbolicator(processing_task)

    def run_symbolicator(self, processing_task):
        # TODO(markus): Make this work with minidumps. An unprocessed minidump
        # event will not contain unsymbolicated frames, because the minidump
        # upload already happened in store.
        # It will also presumably not contain images, so `self.available` will
        # already be `False`.

        if not self.available:
            return

        request_id_cache_key = request_id_cache_key_for_event(self.data)

        stacktraces = []
        processable_stacktraces = []
        has_frames = False

        for stacktrace_info, pf_list in processing_task.iter_processable_stacktraces():
            registers = stacktrace_info.stacktrace.get('registers') or {}

            # The filtering condition of this list comprehension is copied
            # from `iter_processable_frames`.
            #
            # We cannot reuse `iter_processable_frames` because the
            # symbolicator currently expects a list of stacktraces, not
            # flat frames.
            #
            # Right now we can't even filter out frames (e.g. using a frame
            # cache locally). The stacktraces have to be as complete as
            # possible because the symbolicator assumes the first frame of
            # a stacktrace to be the crashing frame. This assumption is
            # already violated because the SDK might chop off frames though
            # (which is less likely to be the case though).
            pf_list = [
                pf for pf in reversed(pf_list)
                if pf.processor == self
            ]

            frames = []

            for pf in pf_list:
                frame = {'instruction_addr': pf['instruction_addr']}
                if pf.get('trust') is not None:
                    frame['trust'] = pf['trust']
                frames.append(frame)
                has_frames = True

            stacktraces.append({
                'registers': registers,
                'frames': frames
            })

            processable_stacktraces.append(pf_list)

        if not has_frames:
            return

        rv = run_symbolicator(
            project=self.project,
            request_id_cache_key=request_id_cache_key,
            stacktraces=stacktraces, modules=self.images,
            signal=self.signal
        )

        if not handle_symbolicator_response_status(self.data, rv):
            return

        # TODO(markus): Set signal and os context from symbolicator response,
        # for minidumps

        assert len(self.images) == len(rv['modules']), (self.images, rv)

        for image, complete_image in zip(self.images, rv['modules']):
            merge_symbolicator_image(
                image, complete_image, self.sdk_info,
                lambda e: handle_symbolication_failed(e, data=self.data)
            )

        assert len(stacktraces) == len(rv['stacktraces'])

        for pf_list, symbolicated_stacktrace in zip(
            processable_stacktraces,
            rv['stacktraces']
        ):
            for symbolicated_frame in symbolicated_stacktrace.get('frames') or ():
                pf = pf_list[symbolicated_frame['original_index']]
                pf.data['symbolicator_match'].append(symbolicated_frame)

    def fetch_ios_system_symbols(self, processing_task):
        to_lookup = []
        pf_list = []
        for pf in processing_task.iter_processable_frames(self):
            if pf.cache_value is not None:
                continue

            obj = pf.data['obj']
            package = obj and obj.code_file
            # TODO(ja): This should check for iOS specifically.  Also
            # check in symbolicator.py for handle_symbolicator_status
            if not package or not is_known_third_party(package):
                continue

            # We can only look up objects in the symbol server that have a
            # uuid.  If we encounter things with an age appended or
            # similar we need to skip.
            try:
                uuid.UUID(obj.debug_id)
            except (ValueError, TypeError):
                continue

            to_lookup.append(
                {
                    'object_uuid': obj.debug_id,
                    'object_name': obj.code_file or '<unknown>',
                    'addr': '0x%x' % rebase_addr(pf.data['instruction_addr'], obj)
                }
            )
            pf_list.append(pf)

        if not to_lookup:
            return

        rv = lookup_system_symbols(to_lookup, self.sdk_info, self.arch)
        if rv is not None:
            for symrv, pf in zip(rv, pf_list):
                if symrv is None:
                    continue
                pf.data['symbolserver_match'] = symrv

    def process_frame(self, processable_frame, processing_task):
        frame = processable_frame.frame
        raw_frame = dict(frame)
        errors = []

        # Ensure that package is set in the raw frame, mapped from the
        # debug_images array in the payload. Grouping and UI can use this path
        # to infer in_app and exclude frames from grouping.
        if raw_frame.get('package') is None:
            obj = processable_frame.data['obj']
            raw_frame['package'] = obj and obj.code_file or None

        if processable_frame.cache_value is None:
            # Construct a raw frame that is used by the symbolizer
            # backend.  We only assemble the bare minimum we need here.
            instruction_addr = processable_frame.data['instruction_addr']

            debug_id = processable_frame.data['debug_id']
            if debug_id is not None:
                self.difs_referenced.add(debug_id)

            try:
                symbolicated_frames = self.sym.symbolize_frame(
                    instruction_addr,
                    self.sdk_info,
                    symbolserver_match=processable_frame.data['symbolserver_match'],
                    symbolicator_match=processable_frame.data.get('symbolicator_match'),
                    trust=raw_frame.get('trust'),
                )
                if not symbolicated_frames:
                    if raw_frame.get('trust') == 'scan':
                        return [], [raw_frame], []
                    else:
                        return None, [raw_frame], []
            except SymbolicationFailed as e:
                errors = []
                handle_symbolication_failed(e, data=self.data, errors=errors)
                return [raw_frame], [raw_frame], errors

            _ignored = None  # Used to be in_app
            processable_frame.set_cache_value([_ignored, symbolicated_frames])

        else:  # processable_frame.cache_value is present
            _ignored, symbolicated_frames = processable_frame.cache_value

        new_frames = []
        for sfrm in symbolicated_frames:
            new_frame = dict(raw_frame)
            merge_symbolicated_frame(new_frame, sfrm)
            new_frames.append(new_frame)

        return new_frames, [raw_frame], []


def reprocess_minidump(data):
    project = Project.objects.get_from_cache(id=data['project'])

    minidump_is_reprocessed_cache_key = minidump_reprocessed_cache_key_for_event(data)
    if default_cache.get(minidump_is_reprocessed_cache_key):
        return

    minidump = get_attached_minidump(data)

    if not minidump:
        logger.error("Missing minidump for minidump event")
        return

    request_id_cache_key = request_id_cache_key_for_event(data)

    response = run_symbolicator(
        project=project,
        request_id_cache_key=request_id_cache_key,
        create_task=create_minidump_task,
        minidump=make_buffered_slice_reader(minidump.data, None)
    )

    if handle_symbolicator_response_status(data, response):
        merge_symbolicator_minidump_response(data, response)

    event_cache_key = cache_key_for_event(data)
    default_cache.set(event_cache_key, dict(data), 3600)
    default_cache.set(minidump_is_reprocessed_cache_key, True, 3600)

    return data


class NativePlugin(Plugin2):
    can_disable = False

    def get_event_enhancers(self, data):
        rv = []
        if is_minidump_event(data):
            rv.append(reprocess_minidump)

        if is_unreal_event(data):
            rv.append(reprocess_unreal_crash)

        return rv

    def get_stacktrace_processors(self, data, stacktrace_infos, platforms, **kwargs):
        if any(platform in NativeStacktraceProcessor.supported_platforms for platform in platforms):
            return [NativeStacktraceProcessor]

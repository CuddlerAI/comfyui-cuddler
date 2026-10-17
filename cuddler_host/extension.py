"""The ComfyUI V3 extension. Nodes arrive with the video, image, speech and music parts."""
import logging

from comfy_api.latest import ComfyExtension

from ..cuddler.errors import CuddlerError
from .runtime import get_runtime

log = logging.getLogger("cuddler")


class CuddlerExtension(ComfyExtension):
    async def on_load(self):
        # When on_load raises, ComfyUI skips get_node_list and drops the whole extension, nodes included.
        # A start-up problem is reported when a node runs, so nothing here may escape.
        try:
            get_runtime().refresh_in_background()
        except CuddlerError as error:  # e.g. a locked user folder
            log.warning("cuddler: local records are unavailable at start-up (%s)", error.code)
        except Exception as error:  # an unforeseen failure must not cost the pack its nodes either
            log.warning("cuddler: start-up work failed (%s)", type(error).__name__)

    async def get_node_list(self):
        from .video import CuddlerVideo
        return [CuddlerVideo]


async def comfy_entrypoint():
    return CuddlerExtension()

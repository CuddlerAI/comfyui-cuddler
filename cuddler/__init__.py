"""Cuddler Platform core for the ComfyUI plugin.

Standard library only, and it never imports ComfyUI: everything here runs and
is tested without a ComfyUI install.
"""

__version__ = "1.0.0"

# Written into every job's metadata so the Platform can count ComfyUI usage.
CLIENT_TAG = "comfyui-cuddler/" + __version__

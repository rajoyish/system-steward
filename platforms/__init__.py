"""OS mechanics behind one import.

``platforms.windows`` holds every Win32 call (ctypes), ``platforms.darwin`` the macOS equivalents.
Business modules decide *what* to do; these modules only know *how* the OS does it. Keep this
package free of heavy imports: the background Keep Awake helper loads it.
"""
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NAME = "Windows" if IS_WINDOWS else "macOS" if IS_MAC else sys.platform

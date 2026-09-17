"""SyncDeck backend package.

No third-party Python dependencies: Decky plugins have to vendor
any wheel into the shipped zip, and every added one is another thing that
can break on a SteamOS update.

The stricter constraint is that Decky runs plugins inside its own
PyInstaller-frozen Python 3.11, which bundles only the stdlib modules Decky
itself imports. `xml.etree` is not among them (verified on hardware), which
is why config.xml is read with regexes. main.py checks every module this
package needs at startup and logs any that are missing.
"""

__version__ = "1.3.0"

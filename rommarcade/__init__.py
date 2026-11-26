"""RomMArcade: an EmuDeck-native arcade front-end for RomM libraries."""

__version__ = "0.1.1"

PRODUCT = "RomMArcade"
PRODUCT_TAGLINE = "Your RomM library, in an arcade shell."
USER_AGENT = f"RomMArcade/{__version__}"
CLIENT_ID = "rommarcade"
# Scopes RomMArcade asks for when it pairs with a server through the device flow.
REQUESTED_SCOPES = [
    "me.read",
    "platforms.read",
    "roms.read",
    "assets.read",
    "firmware.read",
    "devices.read",
    "devices.write",
]

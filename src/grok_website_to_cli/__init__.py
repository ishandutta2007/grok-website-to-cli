"""
grok-website-to-cli
~~~~~~~~~~~~~~~~~~~~

RPA-powered CLI tool that converts Grok website (grok.com) interactions
into a command-line interface using a paired browser extension.
"""

__version__ = "0.1.0"
__author__ = "Ishan Dutta"

from grok_website_to_cli.browser import GrokBridge
from grok_website_to_cli.grok import GrokAutomation

__all__ = ["GrokBridge", "GrokAutomation", "__version__"]

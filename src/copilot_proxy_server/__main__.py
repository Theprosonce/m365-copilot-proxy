"""Enable `python -m copilot_proxy_server [serve|configure|...]` (bare -> serve).

Lets the app run from source on machines where the signed .exe is blocked by Application Control /
Smart App Control — pulled source files carry no Mark-of-the-Web, so the interpreter runs normally.
"""

from .cli import main

if __name__ == "__main__":
    main()

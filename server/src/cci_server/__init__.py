"""The CC-Insights account server.

Two stores behind one HTTP surface, and the reason they are two is the whole
design (docs/ACCOUNTS.md §2):

  * the PERSONAL store holds one person's full rows, paths included, readable
    by exactly one account -- the wire form of what `cc_insights.sync` already
    does straight against PostgreSQL;
  * the TEAM store holds `redact.publication()` and nothing else, keyed on the
    git remote, with no path column in its schema at all.

Collapsing them into one store with a filter on read was considered and
rejected, and not on taste: filtering at query time fails the first time
anything goes wrong -- one API bug, one backup, one `psql` session -- and there
is no un-leaking.

The wire contract is docs/SERVER_API.md and it is frozen.
"""

__version__ = "0.1.0"

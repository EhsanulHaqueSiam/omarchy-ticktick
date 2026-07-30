"""TickTick client for the Omarchy desktop shell plugin `siam.ticktick`.

This package is the entire non-QML half of the plugin: a thin typed wrapper over the
TickTick Open API (:mod:`ticktick.api`), a 0600 credential store (:mod:`ticktick.config`),
sign-in (:mod:`ticktick.auth`), a local task cache and durable write queue that make the
widget instant and usable offline (:mod:`ticktick.store`), date/bucketing helpers
(:mod:`ticktick.dates`), a conservative natural-language quick-add parser
(:mod:`ticktick.nlp`), the row/grouping model the bar widget renders
(:mod:`ticktick.views`), terminal formatting for humans (:mod:`ticktick.render`), and an
argparse front end (:mod:`ticktick.cli`).

It is standard-library only and targets Python 3.11+, because the plugin is installed by
`git clone` into `~/.config/omarchy/plugins/` with no build step and no opportunity to
install dependencies.

Library modules never write to stdout — only :mod:`ticktick.cli` prints. It emits exactly
one JSON object per invocation when stdout is a pipe, which is what QML parses, and a
formatted view when stdout is a terminal.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]

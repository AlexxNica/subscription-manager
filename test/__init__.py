from __future__ import print_function, division, absolute_import

from subscription_manager import ga_loader
ga_loader.init_ga()

from subscription_manager import i18n
i18n.configure_i18n()

from . import rhsm_display
rhsm_display.set_display()

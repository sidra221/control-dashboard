from django import template

from apitokens.i18n import t as translate

register = template.Library()


@register.filter
def period_label(period_value, lang):
    return translate("period_" + period_value, lang)

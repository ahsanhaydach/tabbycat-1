import itertools
import logging
from smtplib import SMTPException

from django.core.mail import get_connection
from django.db.models import Max
from django.template import Template
from django.utils.encoding import force_text
from django.utils.translation import gettext_lazy as _
from django.utils.translation import gettext, pgettext_lazy

from notifications.models import SentMessageRecord
from notifications.utils import TournamentEmailMessage
from utils.misc import reverse_tournament

from .models import Round

logger = logging.getLogger(__name__)

BREAK_ROUND_NAMES = [
    # Translators: abbreviation for "grand final"
    (_("Grand Final"), _("GF")),
    # Translators: abbreviation for "semifinals"
    (_("Semifinals"), _("SF")),
    # Translators: abbreviation for "quarterfinals"
    (_("Quarterfinals"), _("QF")),
    # Translators: abbreviation for "octofinals"
    (_("Octofinals"), _("OF")),
    # Translators: abbreviation for "double-octofinals"
    (_("Double-Octofinals"), _("DOF")),
    # Translators: abbreviation for "triple-octofinals"
    (_("Triple-Octofinals"), _("TOF")),
]

SIDE_NAMES = {
    'aff-neg': {
        "aff_full": _("affirmative"),
        "neg_full": _("negative"),
        "aff_team": _("affirmative team"),
        "neg_team": _("negative team"),
        "aff_abbr": _("Aff"),
        "neg_abbr": _("Neg"),
    },
    'gov-opp': {
        "aff_full": _("government"),
        "neg_full": _("opposition"),
        "aff_team": _("government team"),
        "neg_team": _("opposition team"),
        "aff_abbr": _("Gov"),
        "neg_abbr": _("Opp"),
    },
    'prop-opp': {
        "aff_full": _("proposition"),
        "neg_full": _("opposition"),
        "aff_team": _("proposition team"),
        "neg_team": _("opposition team"),
        "aff_abbr": _("Prop"),
        "neg_abbr": _("Opp"),
    },
    'pro-con': {
        "aff_full": _("pro"),
        "neg_full": _("con"),
        "aff_team": _("pro team"),
        "neg_team": _("con team"),
        "aff_abbr": _("Pro"),
        "neg_abbr": _("Con"),
    },
    'appellant-respondent': {
        "aff_full": _("appellant"),
        "neg_full": _("respondent"),
        "aff_team": _("appellant team"),
        "neg_team": _("respondent team"),
        "aff_abbr": _("App"),
        "neg_abbr": _("Res"),
    },
}

BP_SIDE_NAMES = {  # stop-gap before this system gets refactored
    "og_full": _("opening government"),
    "oo_full": _("opening opposition"),
    "cg_full": _("closing government"),
    "co_full": _("closing opposition"),
    "og_team": _("opening government team"),
    "oo_team": _("opening opposition team"),
    "cg_team": _("closing government team"),
    "co_team": _("closing opposition team"),
    "og_abbr": pgettext_lazy("BP position", "OG"),
    "oo_abbr": pgettext_lazy("BP position", "OO"),
    "cg_abbr": pgettext_lazy("BP position", "CG"),
    "co_abbr": pgettext_lazy("BP position", "CO"),
}


def auto_make_rounds(tournament, num_rounds):
    """Makes the number of rounds specified. The first one is random and the
    rest are all power-paired. The last one is silent. This is intended as a
    convenience function. For anything more complicated, a more advanced import
    method should be used."""
    for i in range(1, num_rounds+1):
        Round(
            tournament=tournament,
            seq=i,
            name=gettext("Round %(number)d") % {'number': i},
            # Translators: This stands for "Round %(number)d".
            abbreviation=gettext("R%(number)d") % {'number': i},
            stage=Round.STAGE_PRELIMINARY,
            draw_type=Round.DRAW_RANDOM if (i == 1) else Round.DRAW_POWERPAIRED,
            feedback_weight=min((i-1)*0.1, 0.5),
            silent=(i == num_rounds),
        ).save()


def auto_make_break_rounds(tournament, num_break, break_category):
    """Makes the number of break rounds specified. This is intended as a
    convenience function. For anything more complicated, a more advanced import
    method should be used."""

    num_prelim = tournament.prelim_rounds().aggregate(Max('seq'))['seq__max']
    # Translators: "UBR" stands for "unknown break round" (used as a fallback when we don't know what it's called)
    break_rounds = itertools.chain(BREAK_ROUND_NAMES, itertools.repeat((_("Unknown break round"), _("UBR"))))

    for i, (name, abbr) in zip(range(num_break), break_rounds):
        Round(
            tournament=tournament,
            break_category=break_category,
            seq=num_prelim+num_break-i,
            stage=Round.STAGE_ELIMINATION,
            name=name,
            abbreviation=abbr,
            draw_type=Round.DRAW_ELIMINATION,
            feedback_weight=0.5,
            silent=True,
        ).save()


def get_side_name_choices():
    """Returns a list of choices for position names suitable for presentation in
    a form."""
    return [
        (code, force_text(names["aff_full"]).capitalize() + ", " + force_text(names["neg_full"]).capitalize())
        for code, names in SIDE_NAMES.items()
    ]


def get_side_name(tournament, side, name_type):
    """Like aff_name, neg_name, etc., but can be used when the side is not known
    at compile time. Example:
        get_side_name(tournament, "aff", "full")
    will return something like "Affirmative" or "Proposition" or "Gobierno",
    depending on the side name option and language setting.
    """
    if side in ('aff', 'neg'):
        names = SIDE_NAMES.get(tournament.pref('side_names'), SIDE_NAMES['aff-neg'])
        return force_text(names["%s_%s" % (side, name_type)])
    elif side in ('og', 'oo', 'cg', 'co'):
        return force_text(BP_SIDE_NAMES["%s_%s" % (side, name_type)])
    else:
        raise ValueError("get_side_name() side must be one of: 'aff', 'neg', 'og', 'oo', 'cg', 'co', not: %r" % (side,))


def _get_side_name(name_type):
    def _wrapped(tournament):
        names = SIDE_NAMES.get(tournament.pref('side_names'), SIDE_NAMES['aff-neg'])
        return force_text(names[name_type])
    return _wrapped


# These functions are used to grab the chosen and translated side names,
# appropriate for the tournament option for side names, and the language
# setting.
#
# For example:              aff-neg, en      prop-opp, en     gov-opp, es
#   aff_name(tournament) -> "Affirmative" or "Proposition" or "Gobierno"
#   neg_abbr(tournament) -> "Neg"         or "Opp"         or "Opo"
#
# They force evaluation, which should be okay, because they can only be used
# when the tournament is known, which is only ever true at runtime.
# Example usage: "The %s team faces the %s team." % (aff_name(tournament), neg_name(tournament))

aff_name = _get_side_name('aff_full')
neg_name = _get_side_name('neg_full')
aff_abbr = _get_side_name('aff_abbr')
neg_abbr = _get_side_name('neg_abbr')
aff_team = _get_side_name('aff_team')
neg_team = _get_side_name('neg_team')


def send_standings_emails(tournament, teams, request, round):
    messages = []

    subject = Template(tournament.pref('team_points_email_subject'))
    message = tournament.pref('team_points_email_message')

    context = {'TOURN': str(tournament)}

    if tournament.pref('public_team_standings'):
        url = request.build_absolute_uri(reverse_tournament('standings-public-teams-current', tournament))
        message += "\n\n" + tournament.pref('team_points_email_link_text') + "\n" + url

    for team in teams:
        context_team = context.copy()
        context_team['POINTS'] = str(team.points_count)
        context_team['TEAM'] = team.short_name

        for speaker in team.speakers:
            if speaker.email is None:
                continue

            context_user = context_team.copy()
            context_user['USER'] = speaker.name

            messages.append(TournamentEmailMessage(subject, Template(message), tournament, round, SentMessageRecord.EVENT_TYPE_POINTS, speaker, context_user))

    try:
        get_connection().send_messages(messages)
    except SMTPException:
        logger.exception("Failed to send team points e-mails")
        raise
    except ConnectionError:
        logger.exception("Connection error sending team points e-mails")
        raise
    else:
        SentMessageRecord.objects.bulk_create([message.as_sent_record() for message in messages])

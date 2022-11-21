import json
import datetime
import logging
from smtplib import SMTPException
import unicodedata
from itertools import product
from math import floor

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.text import format_lazy
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.generic.base import TemplateView

from actionlog.mixins import LogActionMixin
from actionlog.models import ActionLogEntry
from adjallocation.models import DebateAdjudicator
from adjallocation.utils import adjudicator_conflicts_display
from divisions.models import Division
from options.preferences import BPPositionCost
from participants.models import Adjudicator, Institution, Team
from participants.utils import get_side_history
from standings.base import StandingsError
from standings.teams import TeamStandingsGenerator
from standings.views import BaseStandingsView
from tournaments.mixins import (CrossTournamentPageMixin, CurrentRoundMixin,
    DrawForDragAndDropMixin, OptionalAssistantTournamentPageMixin, PublicTournamentPageMixin,
    RoundMixin, TournamentMixin)
from tournaments.models import Round
from tournaments.views import BaseSaveDragAndDropDebateJsonView
from tournaments.utils import get_side_name
from utils.mixins import AdministratorMixin
from utils.views import BadJsonRequestError, PostOnlyRedirectView, VueTableTemplateView
from utils.misc import reverse_round, reverse_tournament
from utils.tables import TabbycatTableBuilder
from venues.allocator import allocate_venues
from venues.models import VenueCategory, VenueConstraint
from venues.utils import venue_conflicts_display

from .dbutils import delete_round_draw
from .generator import DrawFatalError, DrawUserError
from .manager import DrawManager
from .models import Debate, DebateTeam, TeamSideAllocation
from .prefetch import populate_history
from .tables import (AdminDrawTableBuilder, PositionBalanceReportDrawTableBuilder,
        PositionBalanceReportSummaryTableBuilder, PublicDrawTableBuilder)
from .utils import send_mail_to_adjs

logger = logging.getLogger(__name__)


class BasePublicDrawTableView(RoundMixin, VueTableTemplateView):
    """Base class for views showing a draw table to the public in some way.
    Subclasses are *not* necessarily public views; they may be admin/assistant
    views intended to facilitate displaying the draw in the general assembly
    room. Since, whether a public, assistant or admin view, the content on it
    is intended for consumption by the public, the table is always built as if
    it were a public view."""

    template_name = 'draw_display_by.html'
    sort_key = 'venue'

    def get_page_title(self):
        return _("Draw for %(round)s") % {'round': self.round.name}

    def get_page_emoji(self):
        if not self.round:
            return None # Cross-Tournament pages
        elif self.round.draw_status == Round.STATUS_RELEASED:
            return '👏'
        else:
            return '😴'

    def get_page_subtitle(self):
        round = self.round
        if round and round.starts_at:
            return _("debates start at %(time)s (in %(time_zone)s)") % {
                     'time': round.starts_at.strftime('%H:%M'),
                     'time_zone': settings.TIME_ZONE}
        else:
            return ''

    def get_context_data(self, **kwargs):
        kwargs['round'] = self.round
        return super().get_context_data(**kwargs)

    def get_draw(self):
        round = self.round
        draw = round.debate_set_with_prefetches()
        return draw

    def populate_table(self, draw, table, round, tournament, highlight=[]):
        if hasattr(self, 'cross_tournament') and self.cross_tournament is True:
            table.add_tournament_column(d.round.tournament for d in draw) # For cross-tournament draws

        if not round:
            table.add_round_column(d.round for d in draw) # For mass draws

        table.add_debate_venue_columns(draw)
        table.add_debate_team_columns(draw, highlight)

        if tournament.pref('enable_division_motions'):
            table.add_motion_column(d.division_motion for d in draw)

        if not tournament.pref('hide_adjudicators'):
            table.add_debate_adjudicators_column(draw, show_splits=False)

    def get_table(self):
        table = PublicDrawTableBuilder(view=self, sort_key=self.sort_key, admin=False)
        self.populate_table(self.get_draw(), table, self.round, self.tournament)
        return table


# ==============================================================================
# Viewing Draw (Public)
# ==============================================================================

class PublicDrawForRoundView(PublicTournamentPageMixin, BasePublicDrawTableView):

    def is_page_enabled(self, tournament):
        return tournament.pref('public_draw') == 'all-released'

    def get_template_names(self):
        if self.round.draw_status != Round.STATUS_RELEASED:
            return ['draw_not_released.html']
        else:
            return super().get_template_names()

    def get_context_data(self, **kwargs):
        round = self.round
        if round.draw_status != Round.STATUS_RELEASED:
            kwargs["round"] = self.round
            return super(BasePublicDrawTableView, self).get_context_data(**kwargs) # skip BasePublicDrawTableView
        else:
            return super().get_context_data(**kwargs)


class PublicDrawForCurrentRoundView(CurrentRoundMixin, PublicDrawForRoundView):

    def is_page_enabled(self, tournament):
        return tournament.pref('public_draw') == 'current'


class PublicAllDrawsAllTournamentsView(PublicTournamentPageMixin, BasePublicDrawTableView):
    public_page_preference = 'enable_mass_draws'

    @property
    def round(self):
        return None

    def get_page_title(self):
        return _("All Debates for All Rounds of %(tournament)s") % {'tournament': self.tournament.name}

    def get_page_subtitle(self):
        return None

    def get_page_emoji(self):
        return None

    def get_draw(self):
        all_rounds = Round.objects.filter(tournament=self.tournament,
                                          draw_status=Round.STATUS_RELEASED)
        draw = []
        for round in all_rounds:
            draw.extend(round.debate_set_with_prefetches())
        return draw


# ==============================================================================
# Draw Alerts Utilities (Admin)
# ==============================================================================

class AdminDrawUtiltiesMixin:
    """ Shared between the Admin Draw page and Admin Display Page"""

    def get_draw(self):
        if not hasattr(self, '_draw'):
            self._draw = self.round.debate_set_with_prefetches(ordering=('room_rank',),
                    institutions=True, venues=True)
        return self._draw

    @cached_property
    def adjudicator_conflicts(self):
        return adjudicator_conflicts_display(self.get_draw())

    @cached_property
    def venue_conflicts(self):
        return venue_conflicts_display(self.get_draw())

    def get_context_data(self, **kwargs):
        # Need to call super() first, so that get_table() can populate
        # self.venue_conflicts and self.adjudicator_conflicts.
        data = super().get_context_data(**kwargs)

        def _count(conflicts):
            return [len([x for x in c if x[0] != 'success']) > 0 for c in conflicts.values()].count(True)

        data['debates_with_adj_conflicts'] = _count(self.adjudicator_conflicts)
        data['debates_with_venue_conflicts'] = _count(self.venue_conflicts)
        data['active_adjs'] = self.round.active_adjudicators.count()
        data['debates_in_round'] = self.round.debate_set.count()
        if hasattr(self, 'highlighted_cells_exist'):
            data['highlighted_cells_exist'] = self.highlighted_cells_exist
        return data


# ==============================================================================
# Viewing Draw (Admin)
# ==============================================================================

class BaseDrawDisplayView(AdminDrawUtiltiesMixin, RoundMixin, TemplateView):
    pass


class BaseBriefingRoomDrawTableView(BasePublicDrawTableView):
    """Despite the inheritance, this is a base class for views that can be
    accessed only by admins and assistants, and is intended for views of the
    draw that get projected in the briefing room."""

    def get_context_data(self, **kwargs):
        kwargs['no_popovers'] = True  # Note: can't do so in BaseDrawDisplayView
        return super().get_context_data(**kwargs)


class BaseDrawDisplayForRoundByVenueView(BaseBriefingRoomDrawTableView):
    # inherit everything, this class is kept in code for ease of reading
    pass


class BaseDrawDisplayForRoundByTeamView(BaseBriefingRoomDrawTableView):

    sort_key = '' # Leave with default sort order

    def populate_table(self, draw, table, round, tournament):
        # unicodedata.normalize gets accented characters (e.g. "Éothéod") to sort correctly
        draw_by_team = [(debate, debate.get_team(side)) for debate, side in product(draw, tournament.sides)]
        draw_by_team.sort(key=lambda x: unicodedata.normalize('NFKD', table._team_short_name(x[1])))

        if len(draw_by_team) == 0:
            draw, teams = [], []  # next line can't unpack if draw_by_team is empty
        else:
            draw, teams = zip(*draw_by_team)
        super().populate_table(draw, table, round, tournament, highlight=teams)


class AdminDrawDisplayView(AdministratorMixin, BaseDrawDisplayView):
    template_name = 'draw_display_admin.html'


class AdminDrawDisplayForRoundByVenueView(AdministratorMixin, BaseDrawDisplayForRoundByVenueView):
    pass


class AdminDrawDisplayForRoundByTeamView(AdministratorMixin, BaseDrawDisplayForRoundByTeamView):
    pass


class AssistantDrawDisplayView(CurrentRoundMixin, OptionalAssistantTournamentPageMixin, BaseDrawDisplayView):
    template_name = 'draw_display_assistant.html'
    assistant_page_permissions = ['all_areas', 'results_draw']


class AssistantDrawDisplayForRoundByVenueView(CurrentRoundMixin, OptionalAssistantTournamentPageMixin, BaseDrawDisplayForRoundByVenueView):
    assistant_page_permissions = ['all_areas', 'results_draw']


class AssistantDrawDisplayForRoundByTeamView(CurrentRoundMixin, OptionalAssistantTournamentPageMixin, BaseDrawDisplayForRoundByTeamView):
    assistant_page_permissions = ['all_areas', 'results_draw']


# ==============================================================================
# Draw Creation (Admin)
# ==============================================================================

class AdminDrawView(RoundMixin, AdministratorMixin, AdminDrawUtiltiesMixin, VueTableTemplateView):
    detailed = False

    def get_page_title(self):
        round = self.round
        self.page_emoji = '👀'
        if round.draw_status == Round.STATUS_NONE:
            title = _("No Draw")
        elif round.draw_status == Round.STATUS_DRAFT:
            title = _("Draft Draw")
        elif round.draw_status in [Round.STATUS_CONFIRMED, Round.STATUS_RELEASED]:
            self.page_emoji = '👏'
            title = _("Draw")
        else:
            logger.error("Unrecognised draw status: %s", round.draw_status)
            title = _("Draw")
        return title % {'round': round.name}

    def get_bp_position_balance_table(self):
        draw = self.get_draw()
        teams = Team.objects.filter(debateteam__debate__round=self.round)
        side_histories_before = get_side_history(teams, self.tournament.sides, self.round.prev.seq)
        side_histories_now = get_side_history(teams, self.tournament.sides, self.round.seq)
        metrics = self.tournament.pref('team_standings_precedence')
        generator = TeamStandingsGenerator(metrics[0:1], ())
        standings = generator.generate(teams, round=self.round.prev)
        draw_table = PositionBalanceReportDrawTableBuilder(view=self)
        draw_table.build(draw, teams, side_histories_before, side_histories_now, standings)
        self.highlighted_cells_exist = any(draw_table.get_imbalance_category(team) is not None for team in teams)
        return draw_table

    def get_standard_table(self):
        r = self.round

        if r.is_break_round:
            sort_key = "room-rank"
            sort_order = 'asc'
        else:
            sort_key = "bracket"
            sort_order = 'desc'

        table = AdminDrawTableBuilder(view=self, sort_key=sort_key,
                                      sort_order=sort_order,
                                      empty_title=_("No Debates for this Round"))

        draw = self.get_draw()
        populate_history(draw)

        if r.is_break_round:
            table.add_room_rank_columns(draw)
        else:
            table.add_debate_bracket_columns(draw)

        table.add_debate_venue_columns(draw, for_admin=True)
        table.add_debate_team_columns(draw)

        # For draw details and draw draft pages
        if (r.draw_status == Round.STATUS_DRAFT or self.detailed) and r.prev:
            teams = Team.objects.filter(debateteam__debate__round=r)
            metrics = self.tournament.pref('team_standings_precedence')
            # subrank only makes sense if there's a second metric to rank on
            rankings = ('rank', 'subrank') if len(metrics) > 1 else ('rank',)
            generator = TeamStandingsGenerator(metrics, rankings)
            standings = generator.generate(teams, round=r.prev)
            if not r.is_break_round:
                table.add_debate_ranking_columns(draw, standings)
            else:
                self._add_break_rank_columns(table, draw, r.break_category)
            table.add_debate_metric_columns(draw, standings)
            if self.tournament.pref('draw_pullup_restriction') == 'least_to_date':
                table.add_number_of_pullups_columns(draw, r.prev)
            table.add_debate_side_history_columns(draw, r.prev)
        elif not (r.draw_status == Round.STATUS_DRAFT or self.detailed):
            table.add_debate_adjudicators_column(draw, show_splits=False)

        table.add_draw_conflicts_columns(draw, self.venue_conflicts, self.adjudicator_conflicts)

        if not r.is_break_round:
            table.highlight_rows_by_column_value(column=0) # highlight first row of a new bracket

        return table

    def get_table(self):
        r = self.round
        if r.draw_status == Round.STATUS_NONE:
            return TabbycatTableBuilder(view=self)  # blank
        elif self.tournament.pref('teams_in_debate') == 'bp' and \
                r.draw_status == Round.STATUS_DRAFT and r.prev is not None and \
                not r.is_break_round:
            return self.get_bp_position_balance_table()
        else:
            return self.get_standard_table()

    def _add_break_rank_columns(self, table, draw, category):
        for side in self.tournament.sides:
            # Translators: e.g. "Affirmative: Break rank"
            tooltip = _("%(side_name)s: Break rank") % {
                'side_name': get_side_name(self.tournament, side, 'full')
            }
            tooltip = tooltip.capitalize()
            # Translators: "BR" stands for "Break rank"
            key = format_html("{}<br>{}", get_side_name(self.tournament, side, 'abbr'), _("BR"))

            table.add_column(
                {'tooltip': tooltip, 'key': key, 'text': key},
                [d.get_team(side).break_rank_for_category(category) for d in draw]
            )

    def get_template_names(self):
        if self.round.draw_status == Round.STATUS_NONE:
            return ["draw_status_none.html"]
        elif self.round.draw_status == Round.STATUS_DRAFT:
            return ["draw_status_draft.html"]
        elif self.round.draw_status in [Round.STATUS_CONFIRMED, Round.STATUS_RELEASED]:
            return ["draw_status_confirmed.html"]
        else:
            logger.error("Unrecognised draw status: %s", self.round.draw_status)
            return ["base.html"]


class AdminDrawWithDetailsView(AdminDrawView):
    detailed = True
    page_emoji = '👀'
    use_template_subtitle = False  # Use the "for Round n" subtitle

    def get_page_title(self):
        return _("Draw with Details")

    def get_template_names(self):
        return ["draw_subpage.html"]


class PositionBalanceReportView(RoundMixin, AdministratorMixin, VueTableTemplateView):
    page_emoji = "⚖"
    page_title = _("Position Balance Report")
    tables_orientation = 'rows'

    def get_context_data(self, **kwargs):
        kwargs['cost_func'] = self.get_position_cost_function_str()
        return super().get_context_data(**kwargs)

    def get_position_cost_function_str(self):
        cost_func = self.tournament.pref('bp_position_cost')
        if cost_func == 'entropy':
            renyi_order = self.tournament.pref('bp_renyi_order')
            cost_func_str = _("Rényi entropy of order %(order)s" % {'order': renyi_order})
            if renyi_order == 1:
                # Translators: This is appended to the string "Rényi entropy of order 1.0"
                cost_func_str += _(" (<i>i.e.</i>, Shannon entropy)")
            return mark_safe(cost_func_str)
        else:
            for k, v in BPPositionCost.choices:
                if k == cost_func:
                    return v
            else:
                logger.error("Unknown position cost function option: %s", cost_func)
                return "Unknown"  # don't translate, should never happen

    def get_tables(self):
        if self.tournament.pref('teams_in_debate') != 'bp':
            logger.warning("Tried to access position balance report for a non-BP tournament")
            return []
        if self.round.prev is None:
            logger.warning("Tried to access position balance report for first round")
            return []
        if self.round.is_break_round:
            logger.warning("Tried to access position balance report for a break round")
            return []

        draw = self.round.debate_set_with_prefetches(ordering=('room_rank',), institutions=True)
        teams = Team.objects.filter(debateteam__debate__round=self.round)
        side_histories_before = get_side_history(teams, self.tournament.sides, self.round.prev.seq)
        side_histories_now = get_side_history(teams, self.tournament.sides, self.round.seq)
        metrics = self.tournament.pref('team_standings_precedence')
        generator = TeamStandingsGenerator(metrics[0:1], ())
        standings = generator.generate(teams, round=self.round.prev)

        summary_table = PositionBalanceReportSummaryTableBuilder(view=self,
                title=_("Teams with position imbalances"),
                empty_title=_("No teams with position imbalances! Hooray!") + " 😊")
        summary_table.build(draw, teams, side_histories_before, side_histories_now, standings)

        draw_table = PositionBalanceReportDrawTableBuilder(view=self, title=_("Annotated draw"))
        draw_table.build(draw, teams, side_histories_before, side_histories_now, standings)

        return [summary_table, draw_table]

    def get_template_names(self):
        # Show an error page if this isn't a BP tournament or if it's the first round
        if self.tournament.pref('teams_in_debate') != 'bp':
            return ['position_balance_nonbp.html']
        elif self.round.prev is None:
            return ['position_balance_round1.html']
        elif self.round.is_break_round:
            return ['position_balance_break.html']
        else:
            return ['position_balance.html']


# ==============================================================================
# Draw Status POSTS
# ==============================================================================

class DrawStatusEdit(LogActionMixin, AdministratorMixin, RoundMixin, PostOnlyRedirectView):
    round_redirect_pattern_name = 'draw'


class CreateDrawView(DrawStatusEdit):

    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_CREATE

    def post(self, request, *args, **kwargs):
        if self.round.draw_status != Round.STATUS_NONE:
            messages.error(request, _("Could not create draw for %(round)s, there was already a draw!") % {'round': self.round.name})
            return super().post(request, *args, **kwargs)

        try:
            manager = DrawManager(self.round)
            manager.create()
        except DrawUserError as e:
            messages.error(request, mark_safe(_(
                "<p>The draw could not be created, for the following reason: "
                "<em>%(message)s</em></p>\n"
                "<p>Please fix this issue before attempting to create the draw.</p>"
            ) % {'message': str(e)}))
            logger.warning("User error creating draw: " + str(e), exc_info=True)
            return HttpResponseRedirect(reverse_round('availability-index', self.round))
        except DrawFatalError as e:
            messages.error(request, mark_safe(_(
                "The draw could not be created, because the following error occurred: "
                "<em>%(message)s</em></p>\n"
                "<p>If this issue persists and you're not sure how to resolve it, please "
                "contact the developers.</p>"
            ) % {'message': str(e)}))
            logger.exception("Fatal error creating draw: " + str(e))
            return HttpResponseRedirect(reverse_round('availability-index', self.round))
        except StandingsError as e:
            message = _(
                "<p>The team standings could not be generated, because the following error occurred: "
                "<em>%(message)s</em></p>\n"
                "<p>Because generating the draw uses the current team standings, this "
                "prevents the draw from being generated.</p>"
            ) % {'message': str(e)}
            standings_options_url = reverse_tournament('options-tournament-section', self.tournament, kwargs={'section': 'standings'})
            instructions = BaseStandingsView.admin_standings_error_instructions % {'standings_options_url': standings_options_url}
            messages.error(request, mark_safe(message + instructions))
            logger.exception("Error generating standings for draw: " + str(e))
            return HttpResponseRedirect(reverse_round('availability-index', self.round))

        relevant_adj_venue_constraints = VenueConstraint.objects.filter(
                adjudicator__in=self.tournament.relevant_adjudicators)
        if not relevant_adj_venue_constraints.exists():
            allocate_venues(self.round)
        else:
            messages.warning(request, _("Venues were not auto-allocated because there are one or more adjudicator venue constraints. "
                "You should run venue allocations after allocating adjudicators."))

        self.log_action()
        return super().post(request, *args, **kwargs)


class ConfirmDrawCreationView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_CONFIRM

    def post(self, request, *args, **kwargs):
        if self.round.draw_status != Round.STATUS_DRAFT:
            return HttpResponseBadRequest("Draw status is not DRAFT")

        self.round.draw_status = Round.STATUS_CONFIRMED
        self.round.save()
        self.log_action()
        return super().post(request, *args, **kwargs)


class DrawRegenerateView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_REGENERATE
    round_redirect_pattern_name = 'availability-index'

    def post(self, request, *args, **kwargs):
        delete_round_draw(self.round)
        self.log_action()
        messages.success(request, _("Deleted the draw. You can now recreate it as normal."))
        return super().post(request, *args, **kwargs)


class ConfirmDrawRegenerationView(AdministratorMixin, TemplateView):
    template_name = "draw_confirm_regeneration.html"


class DrawReleaseView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_RELEASE
    round_redirect_pattern_name = 'draw-display'

    def post(self, request, *args, **kwargs):
        if self.round.draw_status != Round.STATUS_CONFIRMED:
            return HttpResponseBadRequest("Draw status is not CONFIRMED")

        self.round.draw_status = Round.STATUS_RELEASED
        self.round.save()
        self.log_action()

        email_success_message = ""
        if self.tournament.pref('enable_adj_email'):
            try:
                send_mail_to_adjs(self.round)
            except SMTPException:
                messages.error(self.request, _("There was a problem sending adjudication assignment emails."))
            except ConnectionError as e:
                messages.error(self.request, _(
                    "There was a problem connecting to the e-mail server when trying to send adjudication assigment emails: %(error)s"
                ) % {'error': str(e)})
            else:
                email_success_message = _("Adjudicator emails successfully sent.")

        messages.success(request, format_lazy(_("Released the draw."), " ", email_success_message))
        return super().post(request, *args, **kwargs)


class DrawUnreleaseView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_UNRELEASE
    round_redirect_pattern_name = 'draw-display'

    def post(self, request, *args, **kwargs):
        if self.round.draw_status != Round.STATUS_RELEASED:
            return HttpResponseBadRequest("Draw status is not released")

        self.round.draw_status = Round.STATUS_CONFIRMED
        self.round.save()
        self.log_action()
        messages.success(request, _("Unreleased the draw."))
        return super().post(request, *args, **kwargs)


class SetRoundStartTimeView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_ROUND_START_TIME_SET
    round_redirect_pattern_name = 'draw-display'

    def post(self, request, *args, **kwargs):
        time_text = request.POST["start_time"]
        try:
            time = datetime.datetime.strptime(time_text, "%H:%M").time()
        except ValueError:
            messages.error(request, _("Sorry, \"%(input)s\" isn't a valid time. It must "
                           "be in 24-hour format, with a colon, for "
                           "example: \"13:57\".") % {'input': time_text})
            return super().post(request, *args, **kwargs)

        self.round.starts_at = time
        self.round.save()

        self.log_action()

        return super().post(request, *args, **kwargs)


# ==============================================================================
# Adjudicator Scheduling
# ==============================================================================

class ScheduleDebatesView(AdministratorMixin, RoundMixin, TemplateView):
    template_name = "draw_set_debate_times.html"

    def get_context_data(self, **kwargs):
        vcs = VenueCategory.objects.all()
        for vc in vcs:
            for venue in vc.venues.all():
                debate = Debate.objects.filter(venue=venue, round__tournament=self.tournament, time__isnull=False).first()
                if debate:
                    vc.placeholder_date = debate.time
                    break

        kwargs['venue_categories'] = vcs
        kwargs['divisions'] = Division.objects.filter(tournament=self.tournament).order_by('id')
        return super().get_context_data(**kwargs)


class ScheduleConfirmationsView(AdministratorMixin, RoundMixin, TemplateView):
    template_name = "confirmations_view.html"

    def get_context_data(self, **kwargs):
        adjs = Adjudicator.objects.all().order_by('name')
        for adj in adjs:
            shifts = DebateAdjudicator.objects.filter(adjudicator=adj, debate__round__tournament__active=True)
            if len(shifts) > 0:
                adj.shifts = shifts
        kwargs['adjs'] = adjs
        return super().get_context_data(**kwargs)


class ApplyDebateScheduleView(DrawStatusEdit):

    def post(self, request, *args, **kwargs):
        debates = Debate.objects.filter(round=self.round)
        for debate in debates:
            division = debate.teams[0].division
            if not division:
                continue
            if not division.time_slot:
                continue

            date = request.POST[str(division.venue_category.id)]
            if not date:
                continue

            time = "%s %s" % (date, division.time_slot)
            try:
                debate.time = datetime.datetime.strptime(time,
                                "%Y-%m-%d %H:%M:%S")  # Safari default
            except ValueError:
                pass
            try:
                debate.time = datetime.datetime.strptime(time,
                                "%d/%m/%Y %H:%M:%S")  # Chrome default
            except ValueError:
                pass
            try:
                debate.time = datetime.datetime.strptime(time,
                                "%d/%m/%y %H:%M:%S")  # User typing
            except ValueError:
                pass

            debate.save()

        messages.success(self.request, "Applied schedules to debates")
        return super().post(request, *args, **kwargs)


# ==============================================================================
# Sides Editing and Viewing
# ==============================================================================

class BaseSideAllocationsView(TournamentMixin, VueTableTemplateView):

    page_title = gettext_lazy("Side Pre-Allocations")

    def get_table(self):
        teams = self.tournament.team_set.all()
        rounds = self.tournament.prelim_rounds()

        tsas = dict()
        for tsa in TeamSideAllocation.objects.filter(round__in=rounds):
            try:
                tsas[(tsa.team.id, tsa.round.seq)] = get_side_name(self.tournament, tsa.side, 'abbr')
            except ValueError:
                pass

        table = TabbycatTableBuilder(view=self)
        table.add_team_columns(teams)

        headers = [round.abbreviation for round in rounds]
        data = [[tsas.get((team.id, round.seq), "—") for round in rounds] for team in teams]
        table.add_columns(headers, data)

        return table


class SideAllocationsView(AdministratorMixin, BaseSideAllocationsView):
    pass


class PublicSideAllocationsView(PublicTournamentPageMixin, BaseSideAllocationsView):
    public_page_preference = 'public_side_allocations'


class EditMatchupsView(DrawForDragAndDropMixin, AdministratorMixin, TemplateView):
    template_name = 'edit_matchups.html'
    save_url = "save-debate-teams"

    def annotate_draw(self, draw, serialised_draw):
        if self.round.tournament.pref('teams_in_debate') == 'bp':
            total_possible_rooms = self.round.active_teams.count() / 4
        else:
            total_possible_rooms = self.round.active_teams.count() / 2

        # Make 'fake' debates as placeholders; need a unique ID (hence 9999)
        for i in range(0, floor(total_possible_rooms - len(serialised_draw))):
            serialised_draw.append({
                'id': 999999 + i, 'debateTeams': [], 'debateAdjudicators': [],
                'bracket': 0, 'importance': 0, 'venue': None, 'locked': False
            })

        return super().annotate_draw(draw, serialised_draw)

    def get_context_data(self, **kwargs):
        unused = [t for t in self.round.unused_teams()]
        serialized_unused = [t.serialize() for t in unused]
        break_thresholds = self.break_thresholds
        for t, serialt in zip(unused, serialized_unused):
            serialt = self.annotate_break_classes(serialt, break_thresholds)
            serialt = self.annotate_region_classes(serialt)

        kwargs['vueUnusedTeams'] = json.dumps(serialized_unused)
        kwargs['saveSidesStatusUrl'] = reverse_round('save-debate-sides-status', self.round)
        return super().get_context_data(**kwargs)


class SaveDrawMatchupsView(BaseSaveDragAndDropDebateJsonView):
    action_log_type = ActionLogEntry.ACTION_TYPE_MATCHUP_SAVE
    allows_creation = True

    def modify_debate(self, debate, posted_debate):
        posted_debateteams = posted_debate['debateTeams']

        # Check that all sides are present, and without extras
        sides = [dt['side'] for dt in posted_debateteams]
        if set(sides) != set(self.tournament.sides):
            raise BadJsonRequestError("Sides in JSON object weren't correct")

        # Delete existing entries that won't be wanted (there shouldn't be any, but just in case)
        delete_count, deleted = debate.debateteam_set.exclude(side__in=self.tournament.sides).delete()
        logger.debug("Deleted %d debate teams from [%s]", deleted.get('draw.DebateTeam', 0), debate.matchup)

        # Check that all teams are part of the tournament
        team_ids = [dt['team']['id'] for dt in posted_debateteams]
        teams = Team.objects.filter(tournament=self.tournament, id__in=team_ids)
        if len(teams) != len(posted_debateteams):
            raise BadJsonRequestError("Not all teams specified are associated with the tournament")
        team_name_lookup = {team.id: team.short_name for team in teams}  # for debugging messages

        # Update other DebateTeam objects
        for dt in posted_debateteams:
            team_id = dt['team']['id']
            side = dt['side']
            obj, created = DebateTeam.objects.update_or_create(debate=debate, side=side,
                defaults={'team_id': team_id})
            logger.debug("%s debate team: %s in [%s] is now %s", "Created" if created else "Updated",
                    side, debate.matchup, team_name_lookup[team_id])

        debate._populate_teams()

        return debate


class SaveDebateSidesStatusView(BaseSaveDragAndDropDebateJsonView):
    action_log_type = ActionLogEntry.ACTION_TYPE_SIDES_SAVE
    allows_creation = False

    def modify_debate(self, debate, posted_debate):
        debate.sides_confirmed = posted_debate['sidesStatus']
        debate.save()
        return debate


# ==============================================================================
# Cross-Tournament Draw Views
# ==============================================================================

class AllTournamentsAllInstitutionsView(CrossTournamentPageMixin, TemplateView):
    public_page_preference = 'enable_mass_draws'
    template_name = 'public_all_tournament_institutions.html'

    def get_context_data(self, **kwargs):
        kwargs['institutions'] = Institution.objects.all()
        return super().get_context_data(**kwargs)


class AllTournamentsAllVenuesView(CrossTournamentPageMixin, TemplateView):
    public_page_preference = 'enable_mass_draws'
    template_name = 'public_all_tournament_venues.html'

    def get_context_data(self, **kwargs):
        kwargs['venue_categories'] = VenueCategory.objects.all()
        return super().get_context_data(**kwargs)


class AllDrawsForAllTeamsView(CrossTournamentPageMixin, BasePublicDrawTableView):
    public_page_preference = 'enable_mass_draws'

    def get_page_title(self):
        return _("All Draws for All Teams")

    def get_draw(self):
        draw = Debate.objects.all().select_related('round', 'round__tournament',
                                                   'division')
        return draw


class AllDrawsForInstitutionView(CrossTournamentPageMixin, BasePublicDrawTableView):
    public_page_preference = 'enable_mass_draws'

    def get_institution(self):
        return Institution.objects.get(pk=self.kwargs['institution_id'])

    def get_page_title(self):
        return _("All Debates for Teams from %(institution)s") % {'institution': self.get_institution().name}

    def get_draw(self):
        institution = self.get_institution()
        debate_teams = DebateTeam.objects.filter(
            team__institution=institution).select_related(
            'debate', 'debate__division', 'debate__division__venue_category',
            'debate__round')
        draw = [dt.debate for dt in debate_teams]
        return draw


class AllDrawsForVenueView(CrossTournamentPageMixin, BasePublicDrawTableView):
    public_page_preference = 'enable_mass_draws'

    def get_venue_category(self):
        try:
            return VenueCategory.objects.get(pk=self.kwargs['venue_id'])
        except VenueCategory.DoesNotExist:
            messages.warning(self.request, _("This venue category does not exist "
                "or the URL for it might have changed. Try finding it again "
                "from the homepage."))
            return False

    def get_page_title(self):
        if self.get_venue_category():
            return _("All Debates at %(venue_category)s") % {'venue_category': self.get_venue_category().name}
        else:
            return _("Unknown Venue Category")

    def get_draw(self):
        draw = Debate.objects.filter(
            division__venue_category=self.get_venue_category()).select_related(
            'round', 'round__tournament', 'division')
        return draw

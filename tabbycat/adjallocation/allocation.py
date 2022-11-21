import logging
from warnings import warn

from .models import DebateAdjudicator

logger = logging.getLogger(__name__)


class AdjudicatorAllocation:
    """Class for handling the adjudicators on a panel."""

    POSITION_CHAIR = 'c'
    POSITION_ONLY = 'o'
    POSITION_PANELLIST = 'p'
    POSITION_TRAINEE = 't'

    def __init__(self, debate, chair=None, panellists=None, trainees=None, from_db=False):
        self.debate = debate

        if from_db:
            if chair or panellists or trainees:
                warn("The chair, panellists and trainees arguments are ignored when from_db is used.")
            self.chair = None
            self.panellists = []
            self.trainees = []

            debateadjs = self.debate.debateadjudicator_set.all()

            # The purpose of the line below is to avoid redundant database hits.
            # It uses an internal (undocumented) flag of Django's QuerySet model
            # to detect if there's already a prefetch on it, and avoids causing
            # a clone of the query set (prefetch_related makes a clone), so that
            # it will use the adjudicators that are already prefetched. An
            # important assumption there is that if there's already a prefetch
            # on this debateadjudicator_set, we assume it includes
            # 'adjudicator'. If there isn't a prefetch done, we add a prefetch
            # here to avoid duplicate adjudicator SQLqueries.
            if not debateadjs._prefetch_done:
                debateadjs = debateadjs.prefetch_related('adjudicator')

            for a in debateadjs:
                if a.type == DebateAdjudicator.TYPE_CHAIR:
                    self.chair = a.adjudicator
                elif a.type == DebateAdjudicator.TYPE_PANEL:
                    self.panellists.append(a.adjudicator)
                elif a.type == DebateAdjudicator.TYPE_TRAINEE:
                    self.trainees.append(a.adjudicator)

            # Sort panellists/trainees names for more consistent ballots/prints
            self.panellists.sort(key=lambda adj: adj.name)
            self.trainees.sort(key=lambda adj: adj.name)

        else:
            self.chair = chair
            self.panellists = panellists or []
            self.trainees = trainees or []

    def __len__(self):
        return (0 if self.chair is None else 1) + len(self.panellists) + len(self.trainees)

    def __str__(self):
        items = [str(getattr(x, "name", x)) for x in self.all()]
        return ", ".join(items)

    def __repr__(self):
        result = "<AdjudicatorAllocation for "
        try:
            result += self.debate.matchup
        except AttributeError:
            result += str(self.debate)
        result += ": "
        try:
            result += self.chair.name
        except AttributeError:
            result += str(self.chair)
        result += "; " + ", ".join([p.name for p in self.panellists])
        result += "; " + ", ".join([t.name for t in self.trainees])
        result += ">"
        return result

    def __contains__(self, item):
        return item == self.chair or item in self.panellists or item in self.trainees

    def __eq__(self, other):
        return self.debate == other.debate and \
            self.chair == other.chair and \
            set(self.panellists) == set(other.panellists) and \
            set(self.trainees) == set(other.trainees)

    # ==========================================================================
    # Booleans and other quick properties
    # ==========================================================================

    @property
    def num_voting(self):
        return (0 if self.chair is None else 1) + len(self.panellists)

    @property
    def has_chair(self):
        return self.chair is not None

    @property
    def is_panel(self):
        return len(self.panellists) > 0

    @property
    def is_even(self):
        return self.num_voting % 2 == 0

    @property
    def valid(self):
        return self.has_chair and not self.is_even

    def get_position(self, adj):
        """Returns an AdjudicatorAllocation.POSITION_* constant corresponding
        to the given adjudicator. Returns None if the adjudicator is not on
        this panel."""
        if adj == self.chair:
            if self.is_panel:
                return self.POSITION_CHAIR
            else:
                return self.POSITION_ONLY
        elif adj in self.panellists:
            return self.POSITION_PANELLIST
        elif adj in self.trainees:
            return self.POSITION_TRAINEE
        else:
            return None

    # ==========================================================================
    # Iterators
    # ==========================================================================

    def voting(self):
        """Iterates through voting members of the panel."""
        if self.chair is not None:
            yield self.chair
        for a in self.panellists:
            yield a

    def all(self):
        """Iterates through all members of the panel."""
        for a in self.voting():
            yield a
        for a in self.trainees:
            yield a

    def voting_with_positions(self):
        """Like with_positions(), but only iterates through voting members of
        the panel."""
        if self.chair is not None:
            yield self.chair, self.POSITION_CHAIR if self.is_panel else self.POSITION_ONLY
        for a in self.panellists:
            yield a, self.POSITION_PANELLIST

    def with_positions(self):
        """Iterates through 2-tuples `(adj, type)`, where `type` is one of the
        `AdjudicatorAllocation.POSITION_*` constants (`CHAIR`, `ONLY`,
        `PANELLIST` or `TRAINEE`).

        Note that the `AdjudicatorAllocation.POSITION_*` constants are not
        necessarily the same as the `DebateAdjudicator.TYPE_*` constants."""
        for a, p in self.voting_with_positions():
            yield a, p
        for a in self.trainees:
            yield a, self.POSITION_TRAINEE

    def with_debateadj_types(self):
        if self.chair is not None:
            yield self.chair, DebateAdjudicator.TYPE_CHAIR
        for a in self.panellists:
            yield a, DebateAdjudicator.TYPE_PANEL
        for a in self.trainees:
            yield a, DebateAdjudicator.TYPE_TRAINEE

    # ==========================================================================
    # Database operations
    # ==========================================================================

    def delete(self):
        """Delete existing, current allocation"""
        self.debate.debateadjudicator_set.all().delete()
        self.chair = None
        self.panellists = []
        self.trainees = []

    def save(self):
        self.debate.debateadjudicator_set.exclude(adjudicator__in=self.all()).delete()
        for adj, t in self.with_debateadj_types():
            if not adj:
                continue
            _, created = DebateAdjudicator.objects.update_or_create(debate=self.debate, adjudicator=adj,
                    defaults={'type': t})
            logger.debug("%s: %s, %s, %s", "Created" if created else "Updated", self.debate, adj, t)

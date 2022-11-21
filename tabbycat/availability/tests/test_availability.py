from participants.models import Adjudicator, Institution
from tournaments.models import Round
from utils.tests import BaseDebateTestCase

from ..utils import activate_all, set_availability


class TestAvailability(BaseDebateTestCase):

    def setUp(self):
        super().setUp()
        self.round = Round(tournament=self.t, seq=1)
        self.round.save()

    def tearDown(self):
        super().tearDown()
        self.round.delete()

    def test_all_active(self):
        set_availability(Adjudicator.objects.all(), self.round)
        self.assertEqual(8, Adjudicator.objects.count())
        self.assertEqual(8, self.round.active_adjudicators.count())

    def test_one_disabled(self):
        set_availability(Adjudicator.objects.exclude(name="Adjudicator00"), self.round)
        self.assertEqual(8, Adjudicator.objects.count())
        self.assertEqual(7, self.round.active_adjudicators.count())

    def test_activate_all(self):
        Adjudicator.objects.create(institution=Institution.objects.get(code="INS0"), name="Unattached")
        self.t.preferences['league_options__share_adjs'] = False
        self.t.preferences['league_options__share_venues'] = False
        activate_all(self.round)
        self.assertEqual(8, self.round.active_adjudicators.count())
        self.assertEqual(12, self.round.active_teams.count())
        self.assertEqual(8, self.round.active_venues.count())

    def test_activate_relevant(self):
        Adjudicator.objects.create(institution=Institution.objects.get(code="INS0"), name="Unattached")
        self.t.preferences['league_options__share_adjs'] = True
        self.t.preferences['league_options__share_venues'] = True
        activate_all(self.round)
        self.assertEqual(9, self.round.active_adjudicators.count())
        self.assertEqual(12, self.round.active_teams.count())
        self.assertEqual(16, self.round.active_venues.count())

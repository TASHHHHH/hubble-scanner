import unittest
from alias_supplements import vizier_candidates


class SupplementTests(unittest.TestCase):
    def test_mwsc_ra_is_degrees(self):
        e={'status':'ok','query':{'catalog':'J/A+A/558/A53/catalog'},'data':{'records':[
            {'Name':'Terzan_8','RAJ2000':295.435,'DEJ2000':-33.999,'Type':'g'}]}}
        c=vizier_candidates(e,'MWSC3156')[0]
        self.assertAlmostEqual(c['ra_deg'],295.435,places=3)
        self.assertEqual(c['canonical_designation'],'Terzan 8')

    def test_cns3_separates_components_and_avoids_parent_hd(self):
        e={'status':'ok','query':{'catalog':'V/70A/catalog'},'data':{'records':[
            {'Name':'Gl 127.1','Comp':comp,'RAB1950':'03 10 04','DEB1950':'-68 35 00','HD':123}
            for comp in ['A','B','AB']]}}
        candidates=vizier_candidates(e,'Gl 127.1B')
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['canonical_designation'],'Gl 127.1 B')
        self.assertNotIn('HD 123',candidates[0]['aliases'])


if __name__=='__main__': unittest.main()

import unittest
from publication_audit import classify


class PublicationAuditTests(unittest.TestCase):
    def complete_searches(self):
        return [{'source':s,'status':'ok','complete':True,'match_count':0} for s in
                ['NASA ADS','NASA official image search','ESA official image search']]
    def test_empty_or_incomplete_search_is_not_negative(self):
        self.assertEqual(classify('confirmed',[],[]),'E')
        self.assertEqual(classify('confirmed',[{'status':'ok','complete':False,'match_count':0}],[]),'E')

    def test_paper_hit_is_not_hst_figure(self):
        paper={'kind':'paper','identity_verified':True,'url':'https://example.org/paper','excerpt':'Target mentioned'}
        self.assertEqual(classify('confirmed',[],[paper]),'C')
        self.assertEqual(classify('ambiguous',[],[paper]),'E')

    def test_b_requires_figure_and_release_checks(self):
        paper={'kind':'paper_hst_image','identity_verified':True,'hst_image_verified':True,
               'url':'https://example.org/paper','excerpt':'HST image of target','figure':'Figure 1'}
        self.assertEqual(classify('confirmed',[],[paper]),'E')
        self.assertEqual(classify('confirmed',self.complete_searches(),[paper]),'B')
        del paper['figure']
        self.assertEqual(classify('confirmed',self.complete_searches(),[paper]),'E')

    def test_a_requires_official_identity_verified_image(self):
        evidence={'kind':'official_hst_image','identity_verified':True,'hst_image_verified':True,
                  'url':'https://esahubble.org/images/test/','excerpt':'Hubble image of the verified target'}
        self.assertEqual(classify('confirmed',[],[evidence]),'A')
        evidence['url']='https://example.org/image'
        self.assertEqual(classify('confirmed',[],[evidence]),'E')

    def test_d_requires_complete_successful_zero_searches(self):
        self.assertEqual(classify('confirmed',[{'status':'service_error','complete':True,'match_count':0}],[]),'E')
        self.assertEqual(classify('confirmed',self.complete_searches(),[]),'D')
        self.assertEqual(classify('confirmed',[{'status':'ok','complete':True,'match_count':0}],[]),'E')


if __name__=='__main__': unittest.main()

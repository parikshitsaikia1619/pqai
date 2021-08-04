from collections import Counter
import sys
import re
from pathlib import Path

BASE_DIR = str(Path(__file__).parent.parent.parent.resolve())
THIS_DIR = str(Path(__file__).parent.resolve())
sys.path.append(BASE_DIR)
sys.path.append(THIS_DIR)

from core.api import APIRequest, BadRequestError
from core.api import SearchRequest102, SimilarConceptsRequest
from core.documents import Patent
from core.encoders import default_boe_encoder
from cpc_definitions import CPCDefinitionRetriever

class TextBasedRequest(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._text = req_data['text']

    def _validation_fn(self):
        if not isinstance(self._data.get('text'), str):
            raise BadRequestError('Invalid request')
        if not self._data.get('text').strip():
            raise BadRequestError('Invalid request')

class SuggestCPCs(TextBasedRequest):
    
    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        patents = self._similar_patents()
        cpcs = []
        for patent in patents:
            cpcs += patent.cpcs
        distribution = Counter(cpcs).most_common(10)
        output = []
        for cpc, freq in distribution:
            definition = DefineCPC({'cpc': cpc}).serve()
            output.append({'cpc': cpc, 'definition': definition})
        return output

    def _similar_patents(self):
        search_req = {'q': self._text }
        search_req['after'] = '2015-12-31' # base inference on recent data
        search_req['type'] = 'patent'
        search_req['n'] = 25
        results = SearchRequest102(search_req).serve()['results']
        patents = [Patent(res['id']) for res in results]
        return patents


class PredictGAUs(TextBasedRequest):
    
    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        patents = self._similar_patents()
        pns = [p for p in patents if p.id.endswith(('B1', 'B2', 'A'))]
        gaus = [p.art_unit for p in patents if p.art_unit is not None]
        distribution = Counter(gaus).most_common(3)
        return [gau for gau, freq in distribution]

    def _similar_patents(self):
        search_req = {'q': self._text }
        search_req['after'] = '2015-12-31' # base inference on recent data
        search_req['type'] = 'patent'
        search_req['n'] = 25
        results = SearchRequest102(search_req).serve()['results']
        patents = [Patent(res['id']) for res in results]
        return patents


class SuggestSynonyms(TextBasedRequest):
    
    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        req = SimilarConceptsRequest({'concept': self._text})
        return req.serve()['similar']


class ExtractConcepts(TextBasedRequest):
    
    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        return list(default_boe_encoder.encode(self._text))


class DefineCPC(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._cpc_code = req_data['cpc'].strip()
        self._short = bool(int(req_data.get('short', 0)))
        self._cpc_data = None

    def _validation_fn(self):
        if not isinstance(self._data.get('cpc'), str):
            raise BadRequestError('Invalid request')
        cpc_pattern = r'[ABCDEFGHY]\d\d[A-Z]\d+\/\d+'
        if not re.match(cpc_pattern, self._data.get('cpc').strip()):
            raise BadRequestError('Invalid CPC code')

    def _serving_fn(self):
        segmented = not self._short
        return CPCDefinitionRetriever().define(self._cpc_code, segmented)
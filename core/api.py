from core.vectorizers import SentBERTVectorizer
from core.index_selection import SublassesBasedIndexSelector
from core.filters import FilterArray, PublicationDateFilter, DocTypeFilter
from core.obvious import Combiner
from core.indexes import IndexesDirectory
from core.search import VectorIndexSearcher
from core.documents import Document
from core.snippet import SnippetExtractor, CombinationalMapping
from core.reranking import ConceptMatchRanker
from core.datasets import PoC
from core.documents import Patent
from core.results import SearchResult
import copy

import core.remote as remote
import core.utils as utils

from config.config import indexes_dir, reranker_active, index_selection_disabled
from config.config import allow_outgoing_extension_requests
from config.config import allow_incoming_extension_requests

vectorize_text = SentBERTVectorizer().embed
available_indexes = IndexesDirectory(indexes_dir)
select_indexes = SublassesBasedIndexSelector(available_indexes).select
vector_search = VectorIndexSearcher().search
extract_snippet = SnippetExtractor.extract_snippet
generate_mapping = SnippetExtractor.map
reranker = None if not reranker_active else ConceptMatchRanker()


class APIRequest():
    
    def __init__(self, req_data=None):
        self._data = req_data
        self._validate()

    def serve(self):
        try:
            response = self._serving_fn()
            return self._formatting_fn(response)
        except:
            raise ServerError()

    def _validate(self):
        self._validation_fn()

    def _serving_fn(self):
        pass

    def _validation_fn(self):
        pass

    def _formatting_fn(self, response):
        return response


class BadRequestError(Exception):

    def __init__(self, msg='Invalid request.'):
        self.message = msg


class ServerError(Exception):

    def __init__(self, msg='Server error while handling request.'):
        self.message = msg

class NotAllowedError(Exception):

    def __init__(self, msg="Request disallowed."):
        self.message = msg


class SearchRequest(APIRequest):

    _name = 'Search Request'

    def __init__(self, req_data):
        super().__init__(req_data)
        self._query = req_data.get('q', '')
        self._latent_query = req_data.get('lq', '')
        self._n_results = int(req_data.get('n', 10))
        self._full_query = self._get_full_query()
        self._indexes = self._get_indexes()
        self._need_snippets = self._read_bool_value('snip')
        self._need_mappings = self._read_bool_value('maps')
        self._filters = FilterExtractor(self._data).extract()
        self.MAX_RES_LIMIT = 500

    def __repr__(self):
        return f'{self._name}: {json.dumps(self._data)}'

    def __str__(self):
        return f'[{self._name}]'

    def _serving_fn(self):
        return self._searching_fn()

    def _searching_fn(self):
        pass

    def _get_full_query(self):
        return (self._query + '\n' + self._latent_query).strip()

    def _get_indexes(self):
        if self._index_specified_in_request():
            index_in_req = self._data['idx']
            return available_indexes.get(index_in_req)
        elif index_selection_disabled:
            return list(available_indexes.available())
        else:
            return select_indexes(self._full_query, 3)

    def _index_specified_in_request(self):
        req_data = self._data
        if not 'idx' in req_data:
            return False
        if req_data['idx'] == 'auto':
            return False
        return True

    def _read_bool_value(self, key):
        val = self._data.get(key)
        if ((isinstance(val, str) and val in ['true', '1', 'yes']) or
            (isinstance(val, int) and val != 0)):
            return True
        return False

    def _validation_fn(self):
        if not 'q' in self._data:
            raise BadRequestError(
                'Request does not contain a query.')

    def _add_snippet_if_needed(self, result):
        if self._need_snippets:
            result.snippet = SnippetExtractor.extract_snippet(self._query, result.full_text)


class FilterExtractor():

    def __init__(self, req_data):
        self._data = req_data

    def extract(self):
        filters = FilterArray()
        date_filter = self._get_date_filter()
        doctype_filter = self._get_doctype_filter()
        if date_filter:
            filters.add(date_filter)
        if doctype_filter:
            filters.add(doctype_filter)
        return filters

    def _get_date_filter(self):
        after = self._data.get('after', None)
        before = self._data.get('before', None)
        if after or before:
            after = None if not bool(after) else after
            before = None if not bool(before) else before
            return PublicationDateFilter(after, before)

    def _get_doctype_filter(self):
        doctype = self._data.get('type')
        if doctype:
            return DocTypeFilter(doctype)


class SearchRequest102(SearchRequest):

    _name = '102 Search Request'

    def __init__(self, req_data):
        super().__init__(req_data)

    def _searching_fn(self):
        results = self._get_results()
        results = self._rerank(results)
        return results[:self._n_results]

    def _get_results(self):
        qvec = vectorize_text(self._full_query)
        n = self._n_results
        results = []
        m = n
        while len(results) < n and m < self.MAX_RES_LIMIT:
            results = vector_search(qvec, self._indexes, m)
            results = self._filters.apply(results)
            m *= 2
        return results

    def _add_remote_results_to(self, local_results):
        if not allow_outgoing_extension_requests:
            return local_results
        remote_results = remote.search_extensions(self._data)
        return remote.merge([local_results, remote_results])

    def _rerank(self, results):
        if not reranker:
            return results
        result_texts = [r.abstract for r in results]
        ranks = reranker.rank(self._query, result_texts)
        return [results[i] for i in ranks]

    def _formatting_fn(self, results):
        for result in results:
            self._add_snippet_if_needed(result)
            self._add_mapping_if_needed(result)
        results = [res.json() for res in results]
        results = self._add_remote_results_to(results)
        return {
            'results': results,
            'query': self._query,
            'latent_query': self._latent_query }

    def _add_mapping_if_needed(self, result):
        if self._need_mappings:
            result.mapping = generate_mapping(self._query, result.full_text)


class SearchRequest103(SearchRequest):

    _name = '103 Search Request'

    def __init__(self, req_data):
        super().__init__(req_data)

    def _searching_fn(self):
        docs = self._get_docs_to_combine()
        abstracts = [doc.abstract for doc in docs]
        combiner = Combiner(self._query, abstracts)
        index_pairs = combiner.get_combinations(self._n_results)
        combinations = [(docs[i], docs[j]) for i, j in index_pairs]
        return combinations

    def _get_docs_to_combine(self):
        params = self._get_interim_request_params()
        interim_req = SearchRequest102(params)
        results = interim_req.serve()['results']
        return [SearchResult(r['id'], r['index'], r['score']) for r in results]

    def _get_interim_request_params(self):
        params = self._data.copy()
        params['n'] = 100
        params['maps'] = 0
        params['snip'] = 0
        return params

    def _formatting_fn(self, combinations):
        for combination in combinations:
            for result in combination:
                self._add_snippet_if_needed(result)
            self._add_mapping_if_needed(combination)
        return {
            'results': [[r.json() for r in c] for c in combinations],
            'query': self._query,
            'latent_query': self._latent_query }

    def _add_mapping_if_needed(self, combination):
        if not self._need_mappings:
            return
        for result in combination:
            result.mapping = generate_mapping(self._query, result.full_text)


class SimilarPatentsRequest(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._pn = req_data.get('pn')

    def _serving_fn(self):
        search_request = self._create_text_query_request()
        return SearchRequest102(search_request).serve()

    def _create_text_query_request(self):
        claim = Patent(self._pn).first_claim
        query = utils.remove_claim_number(claim)
        search_request = self._data.copy()
        search_request['q'] = query
        search_request.pop('pn')
        return search_request

    def _validation_fn(self):
        if not utils.is_patent_number(self._data.get('pn')):
            raise BadRequestError(
                'Request does not contain a valid patent number.')

    def _formatting_fn(self, response):
        response['query'] = self._pn
        return response


class PatentPriorArtRequest(SimilarPatentsRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._before = Patent(self._pn).filing_date

    def _serving_fn(self):
        search_request = self._create_text_query_request()
        search_request['before'] = self._before
        return SearchRequest102(search_request).serve()


class DocumentRequest(APIRequest):

    _name = 'Document Request'

    def __init__(self, req_data):
        super().__init__(req_data)
        self._doc_id = req_data['id']

    def _validation_fn(self):
        if not 'id' in self._data:
            raise BadRequestError(
                'Request does not contain a document ID.')

    def _serving_fn(self):
        return Document(self._doc_id).json()


class PassageRequest(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._query = req_data.get('q')
        self._doc_id = req_data.get('pn')
        self._doc = Document(self._doc_id)

    def _validation_fn(self):
        if not self._data.get('q'):
            raise BadRequestError(
                'Request does not contain a query.')
        if not self._data.get('pn'):
            raise BadRequestError(
                'Request does not specify a document.')

class SnippetRequest(PassageRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        query = self._query
        text = self._doc.full_text
        return SnippetExtractor().extract_snippet(query, text)

    def _formatting_fn(self, snippet):
        return {
            'query': self._query,
            'id': self._doc_id,
            'snippet': snippet }


class MappingRequest(PassageRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        query = self._query
        text = self._doc.full_text
        return generate_mapping(query, text)

    def _formatting_fn(self, mapping):
        return {
            'query': self._query,
            'id': self._doc_id,
            'mapping': mapping }

class DatasetSampleRequest(APIRequest):

    poc_dataset = PoC()

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        name = self._data['dataset']
        if name.lower() == 'poc':
            n = self._data['n']
            return self.poc_dataset[int(n)]
        else:
            raise BadRequestError(f'No dataset named {name}.')

    def _validation_fn(self):
        if not 'dataset' in self._data:
            raise BadRequestError(
                'Request does not specify a dataset name.')
        if not 'n' in self._data:
            raise BadRequestError(
                'Request does not specify the sample number.')

    def _formatting_fn(self, sample):
        formatted = {}
        formatted['anc'] = self._format(sample['anc'])
        formatted['pos'] = self._format(sample['pos'])
        formatted['negs'] = [self._format(neg) for neg in sample['negs']]
        return formatted

    def _format(self, pn):
        patent = Patent(pn)
        return {
            'publicationNumber': patent.id,
            'title': patent.title,
            'abstract': patent.abstract
        }

class IncomingExtensionRequest(SearchRequest102):

    def __init__(self, req_data):
        if not allow_incoming_extension_requests:
            raise NotAllowedError(
                'Server does not accept extension requests.')
        else:
            super().__init__(req_data)
import copy
from pathlib import Path
import tempfile
import unittest

import pymupdf

import native_latex as renderer
import render_translation
from typography import ZH_ACADEMIC_V1


class CaptionVectorTests(unittest.TestCase):
    def fixture(self, above=False):
        figure = {'id': 'figure', 'order': 2 if above else 1, 'page': 1,
                  'bbox': [50, 100, 250, 200], 'kind': 'figure',
                  'render_mode': 'source_clip', 'visual_id': 'figure-1', 'font_size': 10}
        caption = {'id': 'caption', 'order': 1 if above else 2, 'page': 1,
                   'bbox': [40, 60, 270, 90] if above else [40, 210, 270, 240],
                   'kind': 'caption', 'render_mode': 'text', 'visual_id': 'figure-1',
                   'source_text': 'Figure 1. CAPTION', 'font_size': 10}
        elements = [figure, caption]
        nodes = render_translation.build_flow_nodes(elements, {'caption': 'RECONSTRUCTED CAPTION'})
        for node in nodes:
            node['visual_layout'] = {'visual_id': 'figure-1', 'caption_id': 'caption',
                                    'source_bbox': figure['bbox'], 'target_width_pt': 230,
                                    'source_internal_font_pt': 10, 'aspect_ratio': 2}
        return nodes, elements

    def test_text_caption_is_single_vector_asset_without_source_mutation(self):
        for above in (False, True):
            with self.subTest(above=above):
                nodes, elements = self.fixture(above)
                original = copy.deepcopy((nodes, elements))
                prepared = renderer.prepare_native_clip_nodes(nodes, elements, ZH_ACADEMIC_V1)
                self.assertEqual(len(prepared), 1)
                self.assertEqual(set(prepared[0]['ids']), {'figure', 'caption'})
                self.assertEqual((nodes, elements), original)
                with tempfile.TemporaryDirectory() as temporary, pymupdf.open() as source:
                    page = source.new_page(width=612, height=792)
                    page.insert_text((60, 140), 'FIGURE CONTENT')
                    caption_y = 78 if above else 228
                    page.insert_text((45, caption_y), 'Figure 1. CAPTION', fontname='hebo', fontsize=10)
                    tex, placements, _ = renderer.build_body_tex(
                        prepared, {}, ZH_ACADEMIC_V1, source, Path(temporary))
                    self.assertNotIn('RECONSTRUCTED CAPTION', tex)
                    self.assertEqual(tex.count('\\includegraphics'), 1)
                    self.assertEqual(len(placements), 1)
                    placement = placements[0]
                    self.assertAlmostEqual(placement['actual_scale'], 1.15)
                    self.assertEqual(placement['caption_id'], 'caption')
                    asset = next((Path(temporary) / 'assets').glob('*.pdf'))
                    with pymupdf.open(asset) as clipped:
                        spans = [span for block in clipped[0].get_text('dict', clip=clipped[0].rect)['blocks']
                                 if 'lines' in block for line in block['lines'] for span in line['spans']]
                        caption_spans = [span for span in spans if 'CAPTION' in span['text']]
                        self.assertEqual(len(caption_spans), 1)
                        self.assertTrue(caption_spans[0]['flags'] & 16)
                        self.assertAlmostEqual(caption_spans[0]['bbox'][0], 5)
                    report = renderer.visual_placement_report({**placement, 'output_page': 1,
                        'bbox': [53, 100, 53 + placement['target_width_pt'], 100 + placement['target_height_pt']]})
                    self.assertAlmostEqual(report['target_width_pt'], 230)
                    self.assertAlmostEqual(report['target_height_pt'], 115)

    def test_rejects_cross_page_or_intervening_body(self):
        for cross_page in (False, True):
            nodes, elements = self.fixture()
            if cross_page:
                nodes[1]['element']['page'] = 2
                nodes[1]['elements'][0]['page'] = 2
            else:
                body = {'id': 'body', 'element': {'kind': 'body'}, 'render_mode': 'text'}
                nodes.insert(1, body)
            with self.assertRaisesRegex(ValueError, 'contiguous same-page'):
                renderer.prepare_native_clip_nodes(nodes, elements, ZH_ACADEMIC_V1)

    def test_caption_can_cross_reading_order_prose_in_a_separate_column(self):
        nodes, elements = self.fixture()
        body = {'id': 'body', 'page': 1, 'bbox': [300, 110, 500, 230],
                'kind': 'body', 'render_mode': 'text', 'translatable': True,
                'source_text': 'UNRELATED LEFT COLUMN'}
        nodes.insert(1, {'id': 'body', 'ids': ['body'], 'element': body,
                         'elements': [body], 'render_mode': 'text'})
        elements.insert(1, body)
        prepared = renderer.prepare_native_clip_nodes(nodes, elements, ZH_ACADEMIC_V1)
        self.assertEqual([node['id'] for node in prepared], ['figure', 'body'])
        self.assertEqual(set(prepared[0]['ids']), {'figure', 'caption'})
        with tempfile.TemporaryDirectory() as temporary, pymupdf.open() as source:
            page = source.new_page(width=612, height=792)
            page.insert_text((60, 140), 'FIGURE CONTENT')
            page.insert_text((45, 228), 'Figure 1. CAPTION')
            page.insert_text((310, 150), 'UNRELATED LEFT COLUMN')
            renderer.build_body_tex(prepared, {}, ZH_ACADEMIC_V1, source, Path(temporary))
            asset = next((Path(temporary) / 'assets').glob('*.pdf'))
            with pymupdf.open(asset) as clipped:
                self.assertIn('CAPTION', clipped[0].get_text())
                self.assertNotIn('UNRELATED', clipped[0].get_text())

    def test_nonadjacent_caption_cannot_swallow_prose_inside_its_crop(self):
        nodes, elements = self.fixture()
        body = {'id': 'body', 'page': 1, 'bbox': [60, 201, 240, 209],
                'kind': 'body', 'render_mode': 'text', 'translatable': True}
        nodes.insert(1, {'id': 'body', 'element': body, 'elements': [body],
                         'render_mode': 'text'})
        with self.assertRaisesRegex(ValueError, 'contiguous same-page'):
            renderer.prepare_native_clip_nodes(nodes, elements, ZH_ACADEMIC_V1)



if __name__ == '__main__':
    unittest.main()

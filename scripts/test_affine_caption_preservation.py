#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Portable caption regressions; defaults to a repository tests/ directory.

Run from anywhere with --scripts-dir PATH/TO/translate-paper/scripts. All PDF
fixtures are generated in memory; no paper-specific files or fonts are needed.
"""
import argparse
import io
import sys
import unittest
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--scripts-dir', type=Path,
                    default=Path(__file__).resolve().parents[1] / 'scripts')
args, rest = parser.parse_known_args()
sys.argv = [sys.argv[0], *rest]
sys.path.insert(0, str(args.scripts_dir))
import check_translation as checker


class AffineCaptionPreservation(unittest.TestCase):
    def setUp(self):
        self.source = fitz.open()
        page = self.source.new_page(width=300, height=400)
        image = Image.new('RGB', (600, 800), 'white')
        draw = ImageDraw.Draw(image)
        for x in range(0, 600, 13):
            draw.line((x, 0, 599 - x, 799), fill=(25, 80, 180), width=2)
        draw.rectangle((130, 220, 460, 550), fill=(240, 140, 30))
        draw.text((130, 570), 'Figure 1. Preserved bitmap caption.', fill='black')
        data = io.BytesIO()
        image.save(data, format='PNG')
        page.insert_image(page.rect, stream=data.getvalue())
        page.insert_text((130, 50), 'CLIPPED SOURCE TEXT', fontsize=10)
        self.source_box = fitz.Rect(60, 90, 240, 310)
        self.target_box = fitz.Rect(52.932, 85.172, 322.934, 415.174)
        self.output = fitz.open()
        target = self.output.new_page(width=595, height=842)
        target.show_pdf_page(self.target_box, self.source, 0,
                             clip=self.source_box, keep_proportion=False)
        self.layout = {'placements': [{
            'id': 'figure', 'ids': ['figure', 'caption'],
            'source_page': 1, 'output_page': 1, 'render_mode': 'source_clip',
            'source_bbox': list(self.source_box), 'bbox': list(self.target_box),
            'caption_id': 'caption',
        }]}

    def tearDown(self):
        self.source.close()
        self.output.close()

    def evidence(self):
        return checker._source_clip_bitmap_noise_evidence(
            self.source, self.output, 0, 0, self.source_box, self.target_box,
            'figure', self.layout, combined_caption=True)

    def identity(self, evidence=None):
        return checker._direct_bitmap_clip_identity(
            self.source, self.output, 0, 0, self.source_box,
            evidence if evidence is not None else self.evidence())

    def test_direct_affine_identity(self):
        evidence = self.evidence()
        self.assertTrue(evidence['bitmap_multisets_identical'])
        self.assertTrue(evidence['bitmap_transforms_match'])
        actual = self.identity(evidence)
        self.assertTrue(actual['direct_affine_pixels_identical'])
        self.assertEqual(actual['direct_affine_source_pixel_sha256'],
                         actual['direct_affine_output_pixel_sha256'])

    def test_each_structural_gate_is_required(self):
        evidence = self.evidence()
        for key in ('bitmap_multisets_identical', 'bitmap_transforms_match',
                    'no_vector_overlays', 'output_to_source_transform'):
            with self.subTest(key=key):
                invalid = {**evidence, key: False}
                self.assertFalse(self.identity(invalid)['direct_affine_pixels_identical'])

    def test_other_form_clipped_text_is_not_a_visible_overlay(self):
        other_source = fitz.Rect(60, 320, 240, 380)
        other_target = fitz.Rect(52.932, 500, 322.934, 590)
        self.output[0].show_pdf_page(other_target, self.source, 0,
                                     clip=other_source, keep_proportion=False)
        self.layout['placements'].append({
            'id': 'code', 'ids': ['code'], 'kind': 'code',
            'source_page': 1, 'output_page': 1, 'render_mode': 'source_clip',
            'source_bbox': list(other_source), 'bbox': list(other_target),
        })
        evidence = self.evidence()
        self.assertFalse(evidence['no_extra_text'])
        self.assertTrue(evidence['bitmap_multisets_identical'])
        self.assertTrue(evidence['bitmap_transforms_match'])
        self.assertTrue(self.identity(evidence)['direct_affine_pixels_identical'])

    def test_actual_form_crop_with_unchanged_bitmap_is_rejected(self):
        page = self.output[0]
        box = fitz.Rect(self.target_box)
        box.x1 = box.x0 + box.width / 2
        content = b'\n'.join(self.output.xref_stream(x) for x in page.get_contents())
        prefix = (f'q {box.x0} {page.rect.height-box.y1} '
                  f'{box.width} {box.height} re W n\n').encode()
        xref = self.output.get_new_xref()
        self.output.update_object(xref, '<<>>')
        self.output.update_stream(xref, prefix + content + b'\nQ')
        page.set_contents(xref)
        evidence = self.evidence()
        self.assertTrue(evidence['bitmap_multisets_identical'])
        self.assertTrue(evidence['bitmap_transforms_match'])
        self.assertFalse(self.identity(evidence)['direct_affine_pixels_identical'])

    def test_visible_text_overlay_is_rejected(self):
        self.output[0].insert_text((90, 130), 'VISIBLE OVERLAY', fontsize=12,
                                   color=(1, 0, 0))
        evidence = self.evidence()
        self.assertTrue(evidence['bitmap_multisets_identical'])
        self.assertTrue(evidence['bitmap_transforms_match'])
        self.assertFalse(evidence['no_extra_text'])
        self.assertFalse(self.identity(evidence)['direct_affine_pixels_identical'])

    def test_visible_vector_overlay_is_rejected(self):
        self.output[0].draw_rect(fitz.Rect(100, 120, 130, 150), fill=(1, 0, 0))
        evidence = self.evidence()
        self.assertFalse(evidence['no_vector_overlays'])
        self.assertFalse(self.identity(evidence)['direct_affine_pixels_identical'])

    def test_replacement_bitmap_is_rejected(self):
        page = self.output[0]
        xref = page.get_image_info(xrefs=True)[0]['xref']
        image = Image.new('RGB', (600, 800), 'white')
        data = io.BytesIO()
        image.save(data, format='PNG')
        page.replace_image(xref, stream=data.getvalue())
        evidence = self.evidence()
        self.assertFalse(evidence['bitmap_multisets_identical'])
        self.assertFalse(self.identity(evidence)['direct_affine_pixels_identical'])


class NativeCaptionExtraction(unittest.TestCase):
    def test_caption_native_order_and_missing_text(self):
        sentence = ('Figure 1. Using 0.5(noise+1) (a) and '
                    '0.8(noise+1) (b) for the intensity of the object.')
        with fitz.open() as output:
            page = output.new_page(width=400, height=300)
            # Content order is correct, while spatial sorting reverses the
            # caption fragments. This models the general extraction-order bug.
            for index, start in enumerate(range(0, len(sentence), 15)):
                page.insert_text((20, 240 - 16 * index),
                                 sentence[start:start + 15], fontsize=10)
            box = fitz.Rect(10, 100, 390, 260)
            layout = {'placements': [{
                'id': 'figure', 'ids': ['figure', 'caption'], 'kind': 'figure',
                'render_mode': 'source_clip', 'caption_id': 'caption',
                'output_page': 1, 'bbox': list(box),
            }]}
            units = [{'id': 'caption', 'kind': 'caption', 'source_text': sentence,
                      'page': 1, 'render_mode': 'text', 'translatable': False}]
            sorted_text = checker.normalize_text(page.get_text(sort=True))
            self.assertIn('caption', checker.missing_invariant_ids(units, sorted_text, layout))
            actual = checker.normalize_text(checker.original_code_output_text(output, layout))
            self.assertNotIn('caption', checker.missing_invariant_ids(units, actual, layout))
            page.add_redact_annot(box, fill=None)
            page.apply_redactions(images=0, graphics=0, text=0)
            removed = checker.normalize_text(checker.original_code_output_text(output, layout))
            self.assertIn('caption', checker.missing_invariant_ids(units, removed, layout))


if __name__ == '__main__':
    unittest.main(verbosity=2)

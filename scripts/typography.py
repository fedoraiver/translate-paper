#!/usr/bin/env python3
"""Shared deterministic typography settings for translated Chinese papers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TypographyProfile:
    name: str
    body_font_size: float
    body_line_pitch: float
    body_letter_spacing: float
    body_text_indent: float
    paragraph_spacing: float
    heading_level_one_size: float
    heading_lower_size: float
    heading_level_one_line_pitch: float
    heading_lower_line_pitch: float
    heading_level_one_before: float
    heading_level_one_after: float
    heading_lower_before: float
    heading_lower_after: float
    list_text_indent: float
    list_padding_left: float
    list_spacing: float
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float


ZH_ACADEMIC_V1 = TypographyProfile(
    name="zh-academic-v1",
    body_font_size=11.5,
    body_line_pitch=16.9,
    body_letter_spacing=0.0,
    body_text_indent=23.0,
    paragraph_spacing=4.0,
    heading_level_one_size=13.8,
    heading_lower_size=11.5,
    heading_level_one_line_pitch=17.9,
    heading_lower_line_pitch=15.0,
    heading_level_one_before=11.5,
    heading_level_one_after=7.0,
    heading_lower_before=9.3,
    heading_lower_after=4.6,
    list_text_indent=-11.5,
    list_padding_left=23.0,
    list_spacing=2.3,
    margin_left=53.0,
    margin_right=53.0,
    margin_top=48.5,
    margin_bottom=55.5,
)

SUPPORTED_PROFILES = {ZH_ACADEMIC_V1.name: ZH_ACADEMIC_V1}


def get_profile(name: str) -> TypographyProfile:
    try:
        return SUPPORTED_PROFILES[name]
    except KeyError as error:
        supported = ", ".join(sorted(SUPPORTED_PROFILES))
        raise ValueError(
            f"Unsupported typography profile {name!r}; supported: {supported}."
        ) from error

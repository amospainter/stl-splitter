// Mirrors stlsplit/connectors.py's SUPPORTED_SHAPES. Small, stable, and the
// backend validates it independently on submit anyway, so a static copy here
// (rather than an extra round-trip to fetch it) is fine.
export const SUPPORTED_SHAPES = ["round", "d", "square", "hex"];

"""Colours shared by the renders and the page."""

# Bambu Studio's Line Type colours (docs/research/native-slicer.md section 15, SOURCE:
# BaseRenderer.cpp). Roles Bambu did not list there are marked ASSUMED.
ROLE_COLOURS = {
    "Inner wall": "#FFE64C", "Outer wall": "#FF7D38", "Overhang wall": "#1F1FFF",
    "Sparse infill": "#B03029", "Internal solid infill": "#9654CC", "Top surface": "#F04040",
    "Bottom surface": "#665CC7", "Bridge": "#4C80BA", "Gap infill": "#FFFFFF",
    "Brim": "#003B6E", "Prime tower": "#B2E3AB", "Custom": "#5ED194", "Flush": "#D9A6F2",
    "Floating vertical shell": "#E6B2B2",
    # ASSUMED
    "Undefined": "#BBBBBB", "Skirt": "#00874E", "Support": "#2FA84F",
    "Support interface": "#1E7A3A", "Support transition": "#4CC06A", "Ironing": "#FF8C69",
    "Internal bridge": "#6A9AD0",
}

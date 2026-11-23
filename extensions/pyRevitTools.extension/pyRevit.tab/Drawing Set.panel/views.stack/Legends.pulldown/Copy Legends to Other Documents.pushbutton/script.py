# pylint: disable=E0401,W0613,C0103,C0111
# -*- coding: utf-8 -*-

from collections import defaultdict

import System
from Autodesk.Revit.DB import Transaction, TransactionGroup
from pyrevit import DB, forms, revit, script
from pyrevit.compat import get_elementid_value_func
from pyrevit.framework import List


SOURCE_DOC = revit.doc
INVALID_ID = DB.ElementId.InvalidElementId
get_elementid_value = get_elementid_value_func()


def id_value(element_id):
    if element_id is None:
        return -1

    return get_elementid_value(element_id)


def escape_format_braces(text):
    return text.replace("{", "{{").replace("}", "}}")


def point_key(point, precision=7):
    if point is None:
        return None

    return (
        round(point.X, precision),
        round(point.Y, precision),
        round(point.Z, precision),
    )


def bbox_key(element, view):
    try:
        box = element.get_BoundingBox(view)

        if box:
            center = (box.Min + box.Max) * 0.5
            size = box.Max - box.Min
            return point_key(center) + point_key(size)
    except Exception:
        pass

    return None


def curve_key(element):
    try:
        curve = element.GeometryCurve
        p0 = point_key(curve.GetEndPoint(0))
        p1 = point_key(curve.GetEndPoint(1))
        ends = tuple(sorted((p0, p1)))
        return curve.GetType().Name, ends
    except Exception:
        return None


def element_signature(element, view):
    if element is None:
        return None

    category_id = id_value(element.Category.Id) if element.Category else -1
    class_name = element.GetType().FullName
    text_value = None

    try:
        text_value = element.Text
    except Exception:
        pass

    location_key = None

    try:
        location = element.Location

        if isinstance(location, DB.LocationPoint):
            location_key = point_key(location.Point)

        elif isinstance(location, DB.LocationCurve):
            curve = location.Curve
            location_key = (
                curve.GetType().Name,
                tuple(
                    sorted(
                        (
                            point_key(curve.GetEndPoint(0)),
                            point_key(curve.GetEndPoint(1)),
                        )
                    )
                ),
            )
    except Exception:
        pass

    type_name = None
    family_name = None

    try:
        element_type = element.Document.GetElement(element.GetTypeId())

        if element_type:
            type_name = element_type.Name

            try:
                family_name = element_type.FamilyName
            except Exception:
                pass
    except Exception:
        pass

    return (
        class_name,
        category_id,
        text_value,
        family_name,
        type_name,
        curve_key(element),
        location_key,
        bbox_key(element, view),
    )


def relaxed_signature(signature):
    if signature is None:
        return None

    class_name, category_id, _, _, _, curve, location, bbox = signature
    return class_name, category_id, curve, location, bbox


def placement_signature(signature):
    if signature is None:
        return None

    class_name, category_id, _, _, _, curve, location, _ = signature

    if curve is None and location is None:
        return None

    return class_name, category_id, curve, location


def pair_equal_signature_groups(source_signatures, destination_signatures):
    source_groups = defaultdict(list)
    destination_groups = defaultdict(list)

    for element_id, signature in source_signatures:
        source_groups[signature].append(element_id)

    for element_id, signature in destination_signatures:
        destination_groups[signature].append(element_id)

    pairs = []
    unmatched_source_values = set()
    matched_destination_values = set()

    for signature, grouped_source_ids in source_groups.items():
        grouped_destination_ids = destination_groups.get(signature, [])

        if signature is not None and len(grouped_source_ids) == len(
            grouped_destination_ids
        ):
            for source_id, destination_id in zip(
                sorted(grouped_source_ids, key=id_value),
                sorted(grouped_destination_ids, key=id_value),
            ):
                pairs.append((source_id, destination_id))
                matched_destination_values.add(id_value(destination_id))
        else:
            unmatched_source_values.update(
                id_value(source_id) for source_id in grouped_source_ids
            )

    remaining_sources = [
        (element_id, signature)
        for element_id, signature in source_signatures
        if id_value(element_id) in unmatched_source_values
    ]
    remaining_destinations = [
        (element_id, signature)
        for element_id, signature in destination_signatures
        if id_value(element_id) not in matched_destination_values
    ]

    return pairs, remaining_sources, remaining_destinations


def match_copied_elements(
    source_doc,
    source_view,
    source_ids,
    destination_doc,
    destination_view,
    destination_ids,
):
    """Pair copied elements with their sources by geometry and content.

    Matching runs in three passes, each over the previous pass's leftovers:
    the full signature; a relaxed one that ignores type, family and text names
    (``CopyElements`` may rename duplicate group or element types); and a
    placement-only one (location point or curve) for elements whose bounding
    box differs because the destination resolved to a different type.

    Important:
        The destination view must already have the source scale and the
        document must be regenerated, or annotation bounding boxes differ.

    Returns:
        tuple: ``(pairs, unmatched_source_ids)``.

    Note:
        Neither ``CopyElements`` nor ``GetMemberIds`` guarantees ordering, so
        nothing is paired by position. A signature is paired only when both
        sides hold the same number of elements; such elements are visually
        indistinguishable, so any pairing between them is equivalent. All
        other source elements are returned as unmatched instead of raising.
    """
    source_signatures = [
        (
            source_id,
            element_signature(source_doc.GetElement(source_id), source_view),
        )
        for source_id in source_ids
    ]
    destination_signatures = [
        (
            destination_id,
            element_signature(
                destination_doc.GetElement(destination_id),
                destination_view,
            ),
        )
        for destination_id in destination_ids
    ]

    pairs, remaining_sources, remaining_destinations = pair_equal_signature_groups(
        source_signatures, destination_signatures
    )

    source_by_value = dict(
        (id_value(element_id), signature) for element_id, signature in source_signatures
    )
    destination_by_value = dict(
        (id_value(element_id), signature)
        for element_id, signature in destination_signatures
    )

    for reduce_signature in (relaxed_signature, placement_signature):
        fallback_pairs, remaining_sources, remaining_destinations = (
            pair_equal_signature_groups(
                [
                    (
                        element_id,
                        reduce_signature(source_by_value[id_value(element_id)]),
                    )
                    for element_id, _ in remaining_sources
                ],
                [
                    (
                        element_id,
                        reduce_signature(destination_by_value[id_value(element_id)]),
                    )
                    for element_id, _ in remaining_destinations
                ],
            )
        )
        pairs.extend(fallback_pairs)

    sole_pairs, remaining_source_ids = pair_sole_elements_by_class(
        [element_id for element_id, _ in remaining_sources],
        [element_id for element_id, _ in remaining_destinations],
        source_by_value,
        destination_by_value,
    )
    pairs.extend(sole_pairs)

    return pairs, remaining_source_ids


def pair_sole_elements_by_class(
    source_ids,
    destination_ids,
    source_by_value,
    destination_by_value,
):
    """Pair leftovers that are the only element of their class and category.

    Covers groups whose copied type differs from the source (for example when
    a member such as a reference plane could not be copied), which changes
    their extents and origin.
    """
    source_groups = defaultdict(list)
    destination_groups = defaultdict(list)

    for element_id in source_ids:
        signature = source_by_value[id_value(element_id)]
        source_groups[signature[:2] if signature else None].append(element_id)

    for element_id in destination_ids:
        signature = destination_by_value[id_value(element_id)]
        destination_groups[signature[:2] if signature else None].append(element_id)

    pairs = []
    remaining_source_ids = []

    for key, grouped_source_ids in source_groups.items():
        grouped_destination_ids = destination_groups.get(key, [])

        if (
            key is not None
            and len(grouped_source_ids) == 1
            and len(grouped_destination_ids) == 1
        ):
            pairs.append((grouped_source_ids[0], grouped_destination_ids[0]))
        else:
            remaining_source_ids.extend(grouped_source_ids)

    return pairs, remaining_source_ids


def rounded_number(value):
    return round(float(value), 10)


def line_pattern_signature(pattern_element):
    """Return the geometric definition of a line pattern."""
    if not pattern_element:
        return None

    pattern = pattern_element.GetLinePattern()
    segments = []

    for segment in pattern.GetSegments():
        segments.append(
            (
                str(segment.Type),
                rounded_number(segment.Length),
            )
        )

    return tuple(segments)


def fill_grid_signature(grid):
    try:
        origin = (
            rounded_number(grid.Origin.U),
            rounded_number(grid.Origin.V),
        )
    except Exception:
        origin = None

    try:
        segments = tuple(rounded_number(value) for value in grid.GetSegments())
    except Exception:
        segments = tuple()

    return (
        rounded_number(grid.Angle),
        rounded_number(grid.Offset),
        rounded_number(grid.Shift),
        origin,
        segments,
    )


def fill_pattern_signature(pattern_element):
    """Return the complete visual definition of a fill pattern."""
    if not pattern_element:
        return None

    pattern = pattern_element.GetFillPattern()

    try:
        grids = sorted(fill_grid_signature(grid) for grid in pattern.GetFillGrids())
    except Exception:
        grids = []

    try:
        orientation = str(pattern.HostOrientation)
    except Exception:
        orientation = None

    return (
        str(pattern.Target),
        orientation,
        bool(pattern.IsSolidFill),
        tuple(grids),
    )


def lowest_id(ids):
    if not ids:
        return None

    return sorted(ids, key=id_value)[0]


def unique_pattern_name(base_name, names, suffix):
    """Return a destination-unique pattern name.

    Important:
        Revit rejects names containing ``{}[]|;<>?`~``, so suffixes use
        parentheses.
    """
    if base_name not in names:
        return base_name

    candidate = "{} ({})".format(
        base_name,
        suffix,
    )
    index = 2

    while candidate in names:
        candidate = "{} ({} {})".format(
            base_name,
            suffix,
            index,
        )
        index += 1

    return candidate


def build_pattern_maps(destination_doc):
    line_by_signature = defaultdict(list)
    line_by_name = defaultdict(list)
    fill_by_signature = defaultdict(list)
    fill_by_name = defaultdict(list)
    fill_solid_by_target = defaultdict(list)
    line_names = set()
    fill_names = set()

    line_patterns = DB.FilteredElementCollector(destination_doc).OfClass(
        DB.LinePatternElement
    )

    for element in line_patterns:
        signature = line_pattern_signature(element)
        line_by_signature[signature].append(element.Id)
        line_by_name[element.Name].append(element.Id)
        line_names.add(element.Name)

    fill_patterns = DB.FilteredElementCollector(destination_doc).OfClass(
        DB.FillPatternElement
    )

    for element in fill_patterns:
        signature = fill_pattern_signature(element)
        fill_by_signature[signature].append(element.Id)
        fill_by_name[element.Name].append(element.Id)
        fill_names.add(element.Name)

        try:
            pattern = element.GetFillPattern()

            if pattern.IsSolidFill:
                fill_solid_by_target[str(pattern.Target)].append(element.Id)
        except Exception:
            pass

    return {
        "destination_doc": destination_doc,
        "line_by_signature": line_by_signature,
        "line_by_name": line_by_name,
        "fill_by_signature": fill_by_signature,
        "fill_by_name": fill_by_name,
        "fill_solid_by_target": fill_solid_by_target,
        "line_names": line_names,
        "fill_names": fill_names,
        "line_cache": {},
        "fill_cache": {},
        "created_line_patterns": [],
        "created_fill_patterns": [],
    }


def clone_line_pattern(
    source_pattern_element,
    pattern_maps,
):
    source_pattern = source_pattern_element.GetLinePattern()
    new_name = unique_pattern_name(
        source_pattern_element.Name,
        pattern_maps["line_names"],
        "pyRevit Imported",
    )
    cloned_pattern = DB.LinePattern(new_name)
    cloned_pattern.SetSegments(source_pattern.GetSegments())

    try:
        created = DB.LinePatternElement.Create(
            pattern_maps["destination_doc"],
            cloned_pattern,
        )
    except Exception as error:
        raise Exception(
            "could not create line pattern '{}' from source '{}': {}".format(
                new_name, source_pattern_element.Name, error
            )
        )
    signature = line_pattern_signature(created)

    pattern_maps["line_names"].add(created.Name)
    pattern_maps["line_by_name"][created.Name].append(created.Id)
    pattern_maps["line_by_signature"][signature].append(created.Id)
    pattern_maps["created_line_patterns"].append(created.Name)

    return created.Id


def clone_fill_grid(source_grid):
    cloned_grid = DB.FillGrid()
    cloned_grid.Angle = source_grid.Angle
    cloned_grid.Offset = source_grid.Offset
    cloned_grid.Shift = source_grid.Shift

    try:
        cloned_grid.Origin = DB.UV(
            source_grid.Origin.U,
            source_grid.Origin.V,
        )
    except Exception:
        pass

    segment_values = List[System.Double]()

    for segment in source_grid.GetSegments():
        segment_values.Add(float(segment))

    cloned_grid.SetSegments(segment_values)

    return cloned_grid


def clone_fill_pattern(
    source_pattern_element,
    pattern_maps,
):
    source_pattern = source_pattern_element.GetFillPattern()
    new_name = unique_pattern_name(
        source_pattern_element.Name,
        pattern_maps["fill_names"],
        "pyRevit Imported",
    )
    cloned_pattern = DB.FillPattern(
        new_name,
        source_pattern.Target,
        source_pattern.HostOrientation,
    )
    cloned_grids = List[DB.FillGrid]()

    for source_grid in source_pattern.GetFillGrids():
        cloned_grids.Add(clone_fill_grid(source_grid))

    cloned_pattern.SetFillGrids(cloned_grids)

    try:
        created = DB.FillPatternElement.Create(
            pattern_maps["destination_doc"],
            cloned_pattern,
        )
    except Exception as error:
        raise Exception(
            "could not create fill pattern '{}' from source '{}': {}".format(
                new_name, source_pattern_element.Name, error
            )
        )
    signature = fill_pattern_signature(created)

    pattern_maps["fill_names"].add(created.Name)
    pattern_maps["fill_by_name"][created.Name].append(created.Id)
    pattern_maps["fill_by_signature"][signature].append(created.Id)
    pattern_maps["created_fill_patterns"].append(created.Name)

    return created.Id


def resolve_line_pattern_id(
    source_doc,
    source_id,
    pattern_maps,
):
    value = id_value(source_id)

    if value == id_value(INVALID_ID):
        return INVALID_ID

    if value == id_value(DB.LinePatternElement.GetSolidPatternId()):
        return DB.LinePatternElement.GetSolidPatternId()

    if value < 0:
        return source_id

    if value in pattern_maps["line_cache"]:
        return pattern_maps["line_cache"][value]

    source_pattern = source_doc.GetElement(source_id)

    if not source_pattern:
        raise Exception("Source line pattern {} was not found.".format(value))

    exact_matches = pattern_maps["line_by_signature"].get(
        line_pattern_signature(source_pattern),
        [],
    )
    destination_id = lowest_id(
        [
            match_id
            for match_id in exact_matches
            if isinstance(
                pattern_maps["destination_doc"].GetElement(match_id),
                DB.LinePatternElement,
            )
        ]
    )

    if destination_id is None:
        destination_id = clone_line_pattern(
            source_pattern,
            pattern_maps,
        )

    pattern_maps["line_cache"][value] = destination_id

    return destination_id


def resolve_fill_pattern_id(
    source_doc,
    source_id,
    pattern_maps,
):
    """Resolve or create a destination fill pattern safely."""
    value = id_value(source_id)

    if value == id_value(INVALID_ID):
        return INVALID_ID

    if value < 0:
        return source_id

    if value in pattern_maps["fill_cache"]:
        return pattern_maps["fill_cache"][value]

    source_pattern_element = source_doc.GetElement(source_id)

    if not source_pattern_element:
        raise Exception("Source fill pattern {} was not found.".format(value))

    source_pattern = source_pattern_element.GetFillPattern()
    signature = fill_pattern_signature(source_pattern_element)
    exact_matches = pattern_maps["fill_by_signature"].get(signature, [])
    destination_id = lowest_id(exact_matches)

    if destination_id is None and source_pattern.IsSolidFill:
        solid_matches = pattern_maps["fill_solid_by_target"].get(
            str(source_pattern.Target),
            [],
        )
        destination_id = lowest_id(solid_matches)

    if destination_id is None:
        destination_id = clone_fill_pattern(
            source_pattern_element,
            pattern_maps,
        )

    pattern_maps["fill_cache"][value] = destination_id

    return destination_id


def expand_group_member_pairs(
    source_doc,
    source_view,
    destination_doc,
    destination_view,
    element_pairs,
):
    """Recursively match copied group members at every nesting depth.

    Returns:
        tuple: ``(expanded_pairs, unmatched_source_ids)``; members that cannot
        be matched unambiguously are reported instead of aborting the legend.
    """
    expanded_pairs = list(element_pairs)
    unmatched_source_ids = []
    pending_pairs = list(element_pairs)
    processed_group_pairs = set()
    existing_pair_keys = set(
        (
            id_value(source_id),
            id_value(destination_id),
        )
        for source_id, destination_id in expanded_pairs
    )

    while pending_pairs:
        source_id, destination_id = pending_pairs.pop()

        source_element = source_doc.GetElement(source_id)
        destination_element = destination_doc.GetElement(destination_id)

        if not isinstance(source_element, DB.Group):
            continue

        if not isinstance(destination_element, DB.Group):
            continue

        group_pair_key = (
            id_value(source_id),
            id_value(destination_id),
        )

        if group_pair_key in processed_group_pairs:
            continue

        processed_group_pairs.add(group_pair_key)

        member_pairs, unmatched_members = match_copied_elements(
            source_doc,
            source_view,
            list(source_element.GetMemberIds()),
            destination_doc,
            destination_view,
            list(destination_element.GetMemberIds()),
        )
        unmatched_source_ids.extend(unmatched_members)

        for (
            member_source_id,
            member_destination_id,
        ) in member_pairs:
            member_pair_key = (
                id_value(member_source_id),
                id_value(member_destination_id),
            )

            if member_pair_key not in existing_pair_keys:
                existing_pair_keys.add(member_pair_key)
                expanded_pairs.append(
                    (
                        member_source_id,
                        member_destination_id,
                    )
                )

            source_member = source_doc.GetElement(member_source_id)
            destination_member = destination_doc.GetElement(member_destination_id)

            if isinstance(source_member, DB.Group) and isinstance(
                destination_member, DB.Group
            ):
                pending_pairs.append(
                    (
                        member_source_id,
                        member_destination_id,
                    )
                )

    return expanded_pairs, unmatched_source_ids


def reference_plane_third_point(reference_plane):
    """Return a third point that defines the source reference plane."""
    bubble_end = reference_plane.BubbleEnd
    free_end = reference_plane.FreeEnd
    line_direction = free_end - bubble_end

    if line_direction.GetLength() <= 1e-9:
        raise Exception("The source reference plane has coincident endpoints.")

    try:
        plane = reference_plane.GetPlane()
        perpendicular = plane.Normal.CrossProduct(line_direction)

        if perpendicular.GetLength() > 1e-9:
            return bubble_end + perpendicular.Normalize()
    except Exception:
        pass

    fallback_vectors = (
        DB.XYZ.BasisX,
        DB.XYZ.BasisY,
        DB.XYZ.BasisZ,
    )

    for fallback_vector in fallback_vectors:
        perpendicular = fallback_vector.CrossProduct(line_direction)

        if perpendicular.GetLength() > 1e-9:
            return bubble_end + perpendicular.Normalize()

    raise Exception("Unable to determine the reference plane orientation.")


def get_reference_plane_names(destination_doc):
    names = set()

    reference_planes = DB.FilteredElementCollector(destination_doc).OfClass(
        DB.ReferencePlane
    )

    for reference_plane in reference_planes:
        try:
            name = reference_plane.Name

            if name:
                names.add(name)
        except Exception:
            pass

    return names


def unique_reference_plane_name(
    base_name,
    existing_names,
):
    if not base_name:
        return None

    if base_name not in existing_names:
        existing_names.add(base_name)
        return base_name

    index = 1
    candidate = "{} (Duplicate {})".format(
        base_name,
        index,
    )

    while candidate in existing_names:
        index += 1
        candidate = "{} (Duplicate {})".format(
            base_name,
            index,
        )

    existing_names.add(candidate)

    return candidate


def recreate_reference_plane(
    source_reference_plane,
    destination_doc,
    destination_view,
    existing_names,
):
    """Recreate a reference plane directly in a destination legend.

    Returns:
        tuple: ``(reference_plane, rename_error)``; ``rename_error`` is
        ``None`` unless the source name could not be applied.
    """
    bubble_end = source_reference_plane.BubbleEnd
    free_end = source_reference_plane.FreeEnd
    third_point = reference_plane_third_point(source_reference_plane)

    destination_reference_plane = destination_doc.Create.NewReferencePlane2(
        bubble_end,
        free_end,
        third_point,
        destination_view,
    )

    try:
        source_name = source_reference_plane.Name
    except Exception:
        source_name = None

    rename_error = None

    if source_name:
        destination_name = unique_reference_plane_name(
            source_name,
            existing_names,
        )

        try:
            destination_reference_plane.Name = destination_name
        except Exception as error:
            rename_error = "Could not rename to '{}': {}".format(
                destination_name,
                error,
            )

    return destination_reference_plane, rename_error


def recreate_reference_planes(
    source_reference_planes,
    destination_doc,
    destination_view,
):
    """Recreate reference planes and return their element pairs."""
    reference_plane_pairs = []
    failures = []
    existing_names = get_reference_plane_names(destination_doc)

    for source_reference_plane in source_reference_planes:
        try:
            destination_reference_plane, rename_error = recreate_reference_plane(
                source_reference_plane,
                destination_doc,
                destination_view,
                existing_names,
            )

            if rename_error:
                failures.append(
                    (
                        source_reference_plane.Id,
                        rename_error,
                    )
                )

            reference_plane_pairs.append(
                (
                    source_reference_plane.Id,
                    destination_reference_plane.Id,
                )
            )

            if id_value(source_reference_plane.GroupId) != id_value(INVALID_ID):
                failures.append(
                    (
                        source_reference_plane.Id,
                        "Recreated outside its source group; the Revit API "
                        "cannot add elements to an existing group.",
                    )
                )
        except Exception as error:
            failures.append(
                (
                    source_reference_plane.Id,
                    str(error),
                )
            )

    return reference_plane_pairs, failures


def has_view_specific_graphics(doc, view, element_id):
    """Tell whether an element carries overrides or hidden state in a view.

    Groups count when any nested member does, since their members are only
    reachable through a matched group.
    """
    element = doc.GetElement(element_id)

    if element is None:
        return False

    try:
        if element.IsHidden(view):
            return True
    except Exception:
        pass

    try:
        ogs = view.GetElementOverrides(element_id)
        has_override = (
            any(
                valid_color(getattr(ogs, name))
                for name in (
                    "ProjectionLineColor",
                    "CutLineColor",
                    "SurfaceForegroundPatternColor",
                    "SurfaceBackgroundPatternColor",
                    "CutForegroundPatternColor",
                    "CutBackgroundPatternColor",
                )
            )
            or ogs.ProjectionLineWeight != -1
            or ogs.CutLineWeight != -1
            or any(
                id_value(getattr(ogs, name)) != id_value(INVALID_ID)
                for name in (
                    "ProjectionLinePatternId",
                    "CutLinePatternId",
                    "SurfaceForegroundPatternId",
                    "SurfaceBackgroundPatternId",
                    "CutForegroundPatternId",
                    "CutBackgroundPatternId",
                )
            )
            or ogs.Halftone
            or ogs.Transparency != 0
            or ogs.DetailLevel != DB.ViewDetailLevel.Undefined
        )

        if has_override:
            return True
    except Exception:
        return True

    if isinstance(element, DB.Group):
        return any(
            has_view_specific_graphics(doc, view, member_id)
            for member_id in element.GetMemberIds()
        )

    return False


def valid_color(color):
    try:
        return color is not None and color.IsValid
    except Exception:
        return False


def record_override_failure(
    failures,
    property_name,
    error,
):
    failures.append("{}: {}".format(property_name, error))


def copy_color_property(
    source_ogs,
    destination_ogs,
    property_name,
    applied,
    failures,
):
    try:
        color = getattr(
            source_ogs,
            property_name,
        )

        if valid_color(color):
            setter = getattr(
                destination_ogs,
                "Set" + property_name,
            )
            setter(
                DB.Color(
                    color.Red,
                    color.Green,
                    color.Blue,
                )
            )
            applied.append(property_name)
    except Exception as error:
        record_override_failure(
            failures,
            property_name,
            error,
        )


def copy_integer_property(
    source_ogs,
    destination_ogs,
    property_name,
    unset_value,
    applied,
    failures,
):
    try:
        value = getattr(
            source_ogs,
            property_name,
        )

        if value != unset_value:
            setter = getattr(
                destination_ogs,
                "Set" + property_name,
            )
            setter(value)
            applied.append(property_name)
    except Exception as error:
        record_override_failure(
            failures,
            property_name,
            error,
        )


def copy_boolean_property(
    source_ogs,
    destination_ogs,
    getter_name,
    setter_name,
    applied,
    failures,
):
    try:
        value = getattr(source_ogs, getter_name)
        setter = getattr(
            destination_ogs,
            setter_name,
        )
        setter(value)
        applied.append(getter_name)
    except AttributeError:
        pass
    except Exception as error:
        record_override_failure(
            failures,
            getter_name,
            error,
        )


def copy_line_pattern_property(
    source_doc,
    source_ogs,
    destination_ogs,
    property_name,
    pattern_maps,
    applied,
    failures,
):
    try:
        source_pattern_id = getattr(
            source_ogs,
            property_name,
        )

        if id_value(source_pattern_id) != id_value(INVALID_ID):
            destination_pattern_id = resolve_line_pattern_id(
                source_doc,
                source_pattern_id,
                pattern_maps,
            )
            setter = getattr(
                destination_ogs,
                "Set" + property_name,
            )

            try:
                setter(destination_pattern_id)
            except Exception as error:
                source_pattern = source_doc.GetElement(source_pattern_id)
                raise Exception(
                    "source pattern '{}' ({}) resolved to destination "
                    "pattern {}: {}".format(
                        source_pattern.Name if source_pattern else "?",
                        id_value(source_pattern_id),
                        id_value(destination_pattern_id),
                        error,
                    )
                )

            applied.append(property_name)
    except Exception as error:
        record_override_failure(
            failures,
            property_name,
            error,
        )


def copy_fill_pattern_property(
    source_doc,
    source_ogs,
    destination_ogs,
    property_name,
    pattern_maps,
    applied,
    failures,
):
    try:
        source_pattern_id = getattr(
            source_ogs,
            property_name,
        )

        if id_value(source_pattern_id) != id_value(INVALID_ID):
            destination_pattern_id = resolve_fill_pattern_id(
                source_doc,
                source_pattern_id,
                pattern_maps,
            )
            setter = getattr(
                destination_ogs,
                "Set" + property_name,
            )
            setter(destination_pattern_id)
            applied.append(property_name)
    except Exception as error:
        record_override_failure(
            failures,
            property_name,
            error,
        )


def apply_destination_overrides(
    source_doc,
    source_view,
    source_id,
    destination_view,
    destination_id,
    pattern_maps,
):
    """Transfer supported element overrides and hidden state.

    Graphic properties are reported as applied only after the destination view
    accepts the completed OverrideGraphicSettings object. Hidden state is
    transferred even when the graphic override write fails.
    """
    source_ogs = source_view.GetElementOverrides(source_id)
    destination_ogs = destination_view.GetElementOverrides(destination_id)
    prepared = []
    failures = []

    for property_name in (
        "ProjectionLineColor",
        "CutLineColor",
        "SurfaceForegroundPatternColor",
        "SurfaceBackgroundPatternColor",
        "CutForegroundPatternColor",
        "CutBackgroundPatternColor",
    ):
        copy_color_property(
            source_ogs,
            destination_ogs,
            property_name,
            prepared,
            failures,
        )

    for property_name in (
        "ProjectionLineWeight",
        "CutLineWeight",
    ):
        copy_integer_property(
            source_ogs,
            destination_ogs,
            property_name,
            -1,
            prepared,
            failures,
        )

    for property_name in (
        "ProjectionLinePatternId",
        "CutLinePatternId",
    ):
        copy_line_pattern_property(
            source_doc,
            source_ogs,
            destination_ogs,
            property_name,
            pattern_maps,
            prepared,
            failures,
        )

    for property_name in (
        "SurfaceForegroundPatternId",
        "SurfaceBackgroundPatternId",
        "CutForegroundPatternId",
        "CutBackgroundPatternId",
    ):
        copy_fill_pattern_property(
            source_doc,
            source_ogs,
            destination_ogs,
            property_name,
            pattern_maps,
            prepared,
            failures,
        )

    for getter_name, setter_name in (
        (
            "IsSurfaceForegroundPatternVisible",
            "SetSurfaceForegroundPatternVisible",
        ),
        (
            "IsSurfaceBackgroundPatternVisible",
            "SetSurfaceBackgroundPatternVisible",
        ),
        (
            "IsCutForegroundPatternVisible",
            "SetCutForegroundPatternVisible",
        ),
        (
            "IsCutBackgroundPatternVisible",
            "SetCutBackgroundPatternVisible",
        ),
    ):
        copy_boolean_property(
            source_ogs,
            destination_ogs,
            getter_name,
            setter_name,
            prepared,
            failures,
        )

    try:
        destination_ogs.SetSurfaceTransparency(source_ogs.Transparency)
        prepared.append("Transparency")
    except Exception as error:
        record_override_failure(
            failures,
            "Transparency",
            error,
        )

    copy_boolean_property(
        source_ogs,
        destination_ogs,
        "Halftone",
        "SetHalftone",
        prepared,
        failures,
    )

    try:
        if source_ogs.DetailLevel != DB.ViewDetailLevel.Undefined:
            destination_ogs.SetDetailLevel(source_ogs.DetailLevel)
            prepared.append("DetailLevel")
    except Exception as error:
        record_override_failure(
            failures,
            "DetailLevel",
            error,
        )

    applied = []

    try:
        destination_view.SetElementOverrides(
            destination_id,
            destination_ogs,
        )
        applied.extend(prepared)
    except Exception as error:
        record_override_failure(
            failures,
            "SetElementOverrides",
            error,
        )

    try:
        source_element = source_doc.GetElement(source_id)

        if source_element and source_element.IsHidden(source_view):
            hidden_ids = List[DB.ElementId]()
            hidden_ids.Add(destination_id)
            destination_view.HideElements(hidden_ids)
            applied.append("ElementVisibility")
    except AttributeError:
        pass
    except Exception as error:
        record_override_failure(
            failures,
            "ElementVisibility",
            error,
        )

    return applied, failures


class CopyUseDestination(DB.IDuplicateTypeNamesHandler):
    def OnDuplicateTypeNamesFound(self, args):
        return DB.DuplicateTypeAction.UseDestinationTypes


class WarningCollector(DB.IFailuresPreprocessor):
    """Collect and dismiss Revit warnings raised while committing a legend.

    Duplicate-type renames from copying groups and nested families cannot be
    redirected to destination definitions through the API, so their warnings
    are recorded for the report instead of interrupting the user. Errors are
    left untouched and still fail the transaction.
    """

    def __init__(self):
        self.messages = []

    def PreprocessFailures(self, failures_accessor):
        for message in failures_accessor.GetFailureMessages():
            if message.GetSeverity() == DB.FailureSeverity.Warning:
                self.messages.append(message.GetDescriptionText())
                failures_accessor.DeleteWarning(message)

        return DB.FailureProcessingResult.Continue


def attach_warning_collector(transaction):
    collector = WarningCollector()
    options = transaction.GetFailureHandlingOptions()
    options.SetFailuresPreprocessor(collector)
    transaction.SetFailureHandlingOptions(options)
    return collector


def can_duplicate_legend(view):
    try:
        return view.CanViewBeDuplicated(DB.ViewDuplicateOption.Duplicate)
    except Exception:
        return False


def source_element_label(element):
    if not element:
        return "Unknown"

    try:
        return element.GetType().Name
    except Exception:
        return "Unknown"


def add_result_row(
    rows,
    doc_title,
    legend_name,
    result,
    element_count,
    override_result,
    message,
):
    rows.append(
        [
            doc_title,
            legend_name,
            result,
            str(element_count),
            override_result,
            message,
        ]
    )


def main():
    logger = script.get_logger()
    output = script.get_output()

    open_docs = forms.select_open_docs(title="Select Destination Documents")

    if not open_docs:
        script.exit()

    legends = forms.select_views(
        title="Select Legend Views",
        filterfunc=lambda view: view.ViewType == DB.ViewType.Legend,
        use_selection=True,
    )

    if not legends:
        forms.alert("No legend views were selected.")
        script.exit()

    output.close_others()
    output.set_title("Copy Legends to Other Documents")
    output.print_md("# Copy Legends to Other Documents")

    total_operations = len(legends) * len(open_docs)

    output.print_md(
        "**Selected legends:** {}  \n"
        "**Destination documents:** {}  \n"
        "**Total operations:** {}".format(
            len(legends),
            len(open_docs),
            total_operations,
        )
    )

    result_rows = []
    override_failure_rows = []
    revit_warning_rows = []
    skipped_element_rows = []

    current_operation = 0
    succeeded_count = 0
    warning_count = 0
    failed_count = 0
    skipped_count = 0
    cancelled = False

    with forms.ProgressBar(
        title="Copying legends {value} of {max_value}",
        cancellable=True,
        step=1,
    ) as progress:
        for dest_doc in open_docs:
            if cancelled:
                break

            all_graphviews = revit.query.get_all_views(doc=dest_doc)
            all_legend_names = [
                revit.query.get_name(view)
                for view in all_graphviews
                if view.ViewType == DB.ViewType.Legend
            ]
            base_legend = revit.query.find_first_legend(doc=dest_doc)

            if not base_legend or not can_duplicate_legend(base_legend):
                if base_legend:
                    reason = (
                        "No legend capable of duplication "
                        "exists in the destination document."
                    )
                else:
                    reason = "No base legend exists in the destination document."

                logger.warning(
                    "Skipping destination document '%s': %s",
                    dest_doc.Title,
                    reason,
                )

                for src_legend in legends:
                    legend_name = revit.query.get_name(src_legend)

                    add_result_row(
                        result_rows,
                        dest_doc.Title,
                        legend_name,
                        "Skipped",
                        0,
                        "Not attempted",
                        reason,
                    )

                    skipped_count += 1
                    current_operation += 1

                    progress.update_progress(
                        current_operation,
                        total_operations,
                    )

                continue

            transaction_group = TransactionGroup(
                dest_doc,
                "Copy Legends to document",
            )
            transaction_group.Start()
            destination_had_commit = False

            try:
                for src_legend in legends:
                    if progress.cancelled:
                        cancelled = True
                        break

                    legend_name = revit.query.get_name(src_legend)
                    operation_number = current_operation + 1

                    progress.title = "{} of {} | {} | {}".format(
                        operation_number,
                        total_operations,
                        escape_format_braces(dest_doc.Title),
                        escape_format_braces(legend_name),
                    )
                    progress.update_progress(
                        operation_number,
                        total_operations,
                    )

                    logger.info(
                        "Copying legend '%s' to document '%s'.",
                        legend_name,
                        dest_doc.Title,
                    )

                    view_elements = (
                        DB.FilteredElementCollector(SOURCE_DOC)
                        .OwnedByView(src_legend.Id)
                        .WhereElementIsNotElementType()
                        .ToElements()
                    )

                    candidate_ids = set(
                        id_value(element.Id)
                        for element in view_elements
                        if (element and element.Category is not None)
                    )

                    skipped_labels = defaultdict(int)
                    source_ids = []
                    source_reference_planes = []
                    grouped_member_count = 0

                    for element in view_elements:
                        if not element:
                            continue

                        if element.Category is None:
                            skipped_labels[source_element_label(element)] += 1
                            continue

                        if isinstance(
                            element,
                            DB.ReferencePlane,
                        ):
                            source_reference_planes.append(element)
                            continue

                        group_id = element.GroupId

                        is_group_member = (
                            id_value(group_id) != id_value(INVALID_ID)
                            and id_value(group_id) in candidate_ids
                        )

                        if is_group_member:
                            grouped_member_count += 1
                            continue

                        source_ids.append(element.Id)

                    if skipped_labels:
                        skipped_element_rows.append(
                            [
                                dest_doc.Title,
                                legend_name,
                                str(sum(skipped_labels.values())),
                                "No category, not copied: {}".format(
                                    ", ".join(
                                        "{} x{}".format(label, count)
                                        for label, count in sorted(
                                            skipped_labels.items()
                                        )
                                    )
                                ),
                            ]
                        )

                    if grouped_member_count:
                        logger.info(
                            "Legend '%s': %s group member(s) "
                            "will be copied through their "
                            "parent group.",
                            legend_name,
                            grouped_member_count,
                        )

                    if not source_ids and not source_reference_planes:
                        reason = (
                            "The source legend contains no supported elements to copy."
                        )

                        logger.warning(
                            "Skipping legend '%s' for '%s': %s",
                            legend_name,
                            dest_doc.Title,
                            reason,
                        )

                        add_result_row(
                            result_rows,
                            dest_doc.Title,
                            legend_name,
                            "Skipped",
                            0,
                            "Not attempted",
                            reason,
                        )

                        skipped_count += 1
                        current_operation += 1

                        progress.update_progress(
                            current_operation,
                            total_operations,
                        )

                        continue

                    transaction = Transaction(
                        dest_doc,
                        "Copy legend and graphic overrides",
                    )
                    transaction.Start()
                    warning_collector = attach_warning_collector(transaction)

                    copied_ids = []
                    reference_plane_pairs = []
                    transferred_count = 0
                    failed_override_count = 0

                    try:
                        destination_view_id = base_legend.Duplicate(
                            DB.ViewDuplicateOption.Duplicate
                        )
                        dest_view = dest_doc.GetElement(destination_view_id)
                        dest_view.Scale = src_legend.Scale

                        options = DB.CopyPasteOptions()
                        options.SetDuplicateTypeNamesHandler(CopyUseDestination())

                        ordered_source_ids = List[DB.ElementId]()

                        for source_id in source_ids:
                            ordered_source_ids.Add(source_id)

                        source_ids = [element_id for element_id in ordered_source_ids]

                        if source_ids:
                            copied_collection = DB.ElementTransformUtils.CopyElements(
                                src_legend,
                                ordered_source_ids,
                                dest_view,
                                None,
                                options,
                            )

                            copied_ids = [
                                element_id for element_id in copied_collection
                            ]
                            dest_doc.Regenerate()

                        pattern_maps = build_pattern_maps(dest_doc)
                        element_pairs = []
                        unmatched_source_ids = []

                        if source_ids:
                            (
                                element_pairs,
                                unmatched_source_ids,
                            ) = match_copied_elements(
                                SOURCE_DOC,
                                src_legend,
                                source_ids,
                                dest_doc,
                                dest_view,
                                copied_ids,
                            )

                            (
                                element_pairs,
                                unmatched_members,
                            ) = expand_group_member_pairs(
                                SOURCE_DOC,
                                src_legend,
                                dest_doc,
                                dest_view,
                                element_pairs,
                            )
                            unmatched_source_ids.extend(unmatched_members)

                        unmatched_without_graphics = 0

                        for unmatched_source_id in unmatched_source_ids:
                            if not has_view_specific_graphics(
                                SOURCE_DOC, src_legend, unmatched_source_id
                            ):
                                unmatched_without_graphics += 1
                                continue

                            failed_override_count += 1
                            override_failure_rows.append(
                                [
                                    dest_doc.Title,
                                    legend_name,
                                    str(id_value(unmatched_source_id)),
                                    "",
                                    source_element_label(
                                        SOURCE_DOC.GetElement(unmatched_source_id)
                                    ),
                                    "Copied, but could not be matched "
                                    "unambiguously; overrides were not "
                                    "transferred.",
                                ]
                            )

                        if unmatched_without_graphics:
                            logger.info(
                                "Legend '%s': %s copied element(s) could not "
                                "be matched but carry no overrides.",
                                legend_name,
                                unmatched_without_graphics,
                            )

                        (
                            reference_plane_pairs,
                            reference_plane_failures,
                        ) = recreate_reference_planes(
                            source_reference_planes,
                            dest_doc,
                            dest_view,
                        )

                        if not copied_ids and not reference_plane_pairs:
                            raise Exception(
                                "No elements were copied or "
                                "recreated in the destination legend."
                            )

                        element_pairs.extend(reference_plane_pairs)

                        for (
                            source_reference_plane_id,
                            reference_plane_error,
                        ) in reference_plane_failures:
                            failed_override_count += 1

                            override_failure_rows.append(
                                [
                                    dest_doc.Title,
                                    legend_name,
                                    str(id_value(source_reference_plane_id)),
                                    "",
                                    "ReferencePlane",
                                    (
                                        "Reference plane recreation: {}".format(
                                            reference_plane_error
                                        )
                                    ),
                                ]
                            )

                            logger.warning(
                                "Reference plane %s could not "
                                "be recreated for legend '%s' "
                                "in '%s': %s",
                                id_value(source_reference_plane_id),
                                legend_name,
                                dest_doc.Title,
                                reference_plane_error,
                            )

                        for (
                            source_id,
                            destination_id,
                        ) in element_pairs:
                            try:
                                (
                                    applied_properties,
                                    property_failures,
                                ) = apply_destination_overrides(
                                    SOURCE_DOC,
                                    src_legend,
                                    source_id,
                                    dest_view,
                                    destination_id,
                                    pattern_maps,
                                )

                                if applied_properties:
                                    transferred_count += 1

                                if property_failures:
                                    failed_override_count += 1
                                    source_element = SOURCE_DOC.GetElement(source_id)

                                    for property_failure in property_failures:
                                        override_failure_rows.append(
                                            [
                                                dest_doc.Title,
                                                legend_name,
                                                str(id_value(source_id)),
                                                str(id_value(destination_id)),
                                                source_element_label(source_element),
                                                property_failure,
                                            ]
                                        )

                                        logger.warning(
                                            "Graphic override property "
                                            "failed for legend '%s', "
                                            "source element %s, "
                                            "destination element %s "
                                            "in '%s': %s",
                                            legend_name,
                                            id_value(source_id),
                                            id_value(destination_id),
                                            dest_doc.Title,
                                            property_failure,
                                        )

                            except Exception as override_error:
                                failed_override_count += 1
                                source_element = SOURCE_DOC.GetElement(source_id)

                                override_failure_rows.append(
                                    [
                                        dest_doc.Title,
                                        legend_name,
                                        str(id_value(source_id)),
                                        str(id_value(destination_id)),
                                        source_element_label(source_element),
                                        str(override_error),
                                    ]
                                )

                                logger.warning(
                                    "Graphic override failed for "
                                    "legend '%s', source element %s, "
                                    "destination element %s "
                                    "in '%s': %s",
                                    legend_name,
                                    id_value(source_id),
                                    id_value(destination_id),
                                    dest_doc.Title,
                                    override_error,
                                )

                        new_name = legend_name
                        counter = 0

                        while new_name in all_legend_names:
                            counter += 1
                            new_name = legend_name + " (Duplicate {})".format(counter)

                        revit.update.set_name(
                            dest_view,
                            new_name,
                        )
                        all_legend_names.append(new_name)

                        commit_status = transaction.Commit()

                        if commit_status != DB.TransactionStatus.Committed:
                            raise Exception(
                                "The destination transaction did not "
                                "commit (status: {}).".format(commit_status)
                            )

                        destination_had_commit = True

                        warning_counts = defaultdict(int)

                        for warning_text in warning_collector.messages:
                            warning_counts[warning_text] += 1

                        for warning_text, count in sorted(warning_counts.items()):
                            revit_warning_rows.append(
                                [dest_doc.Title, new_name, str(count), warning_text]
                            )

                        if failed_override_count:
                            warning_count += 1
                            result_label = "Copied with warnings"
                            message = (
                                "Created as '{}'. Some elements, "
                                "reference planes, or overrides "
                                "could not be transferred.".format(new_name)
                            )

                            logger.warning(
                                "Legend '%s' copied to '%s' with %s failure(s).",
                                legend_name,
                                dest_doc.Title,
                                failed_override_count,
                            )
                        else:
                            succeeded_count += 1
                            result_label = "Copied"
                            message = "Created as '{}'.".format(new_name)

                            logger.info(
                                "Legend '%s' copied successfully to '%s' as '%s'.",
                                legend_name,
                                dest_doc.Title,
                                new_name,
                            )

                        add_result_row(
                            result_rows,
                            dest_doc.Title,
                            legend_name,
                            result_label,
                            (len(copied_ids) + len(reference_plane_pairs)),
                            "{} applied / {} failed".format(
                                transferred_count,
                                failed_override_count,
                            ),
                            message,
                        )

                    except Exception as legend_error:
                        if transaction.GetStatus() == DB.TransactionStatus.Started:
                            transaction.RollBack()

                        failed_count += 1

                        add_result_row(
                            result_rows,
                            dest_doc.Title,
                            legend_name,
                            "Failed",
                            0,
                            "Not completed",
                            str(legend_error),
                        )

                        logger.exception(
                            "Legend '%s' could not be copied to '%s'.",
                            legend_name,
                            dest_doc.Title,
                        )

                    current_operation += 1
                    progress.update_progress(
                        current_operation,
                        total_operations,
                    )

                if cancelled:
                    if destination_had_commit:
                        transaction_group.Assimilate()
                    else:
                        transaction_group.RollBack()
                else:
                    transaction_group.Assimilate()

            except Exception:
                if transaction_group.GetStatus() == DB.TransactionStatus.Started:
                    transaction_group.RollBack()

                logger.exception(
                    "Unexpected destination-level failure in '%s'.",
                    dest_doc.Title,
                )
                raise

    output.print_table(
        table_data=result_rows,
        columns=[
            "Destination",
            "Legend",
            "Result",
            "Elements",
            "Overrides",
            "Message",
        ],
        title="Copy Results",
    )

    if override_failure_rows:
        output.print_table(
            table_data=override_failure_rows,
            columns=[
                "Destination",
                "Legend",
                "Source ID",
                "Destination ID",
                "Element Type",
                "Failure",
            ],
            title="Graphic and Recreation Failures",
        )

    if revit_warning_rows:
        output.print_table(
            table_data=revit_warning_rows,
            columns=[
                "Destination",
                "Legend",
                "Count",
                "Warning",
            ],
            title="Revit Warnings Dismissed",
        )

    if skipped_element_rows:
        output.print_table(
            table_data=skipped_element_rows,
            columns=[
                "Destination",
                "Legend",
                "Count",
                "Reason",
            ],
            title="Elements Intentionally Skipped",
        )

    completed_count = succeeded_count + warning_count

    if cancelled:
        status_message = "Copy operation cancelled."
    else:
        status_message = "Legend copy operation complete."

    summary = (
        "Completed: {}\n"
        "Copied successfully: {}\n"
        "Copied with warnings: {}\n"
        "Failed: {}\n"
        "Skipped: {}\n"
        "Override or recreation failures: {}\n\n"
        "See the pyRevit output window for details."
    ).format(
        completed_count,
        succeeded_count,
        warning_count,
        failed_count,
        skipped_count,
        len(override_failure_rows),
    )

    forms.alert(
        status_message,
        sub_msg=summary,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

# Calibration utility for myweather Weather HAT corrections.
#
# Author: Carlos Gil (ea1ii)
# Date: 2026-09-30
# Version: 0.2
# Compatible with CGweather: 0.4.1
# License: MIT (see ../LICENSE)
# GitHub: https://github.com/ea1ii/myweather
#
# Description: Fits temperature and humidity correction polynomials from
#              logged raw readings and local reference samples.
#
# Usage: ./src/calibrate.py --help

import argparse
import bisect
import csv
from html import escape
import json
import math
import os
import stat
import statistics
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from math import comb
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
CALIBRATE_VERSION = "0.2"
CHANNELS = {
    "temperature": {
        "input_field": "raw_temperature_celsius",
        "reference_index": 1,
        "config_key": "temperature",
        "label": "Temperature",
        "outlier_min_deviation": 0.5,
    },
    "humidity": {
        "input_field": "raw_humidity_percent",
        "reference_index": 2,
        "config_key": "humidity",
        "label": "Humidity",
        "outlier_min_deviation": 2.0,
    },
}


def parse_timestamp(value):
    # Datalog timestamps include a UTC offset/Z. If one is naive, assume UTC,
    # then convert to local time to match the calibration-file timestamps.
    timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone()


def read_datalog(path):
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as input_file:
        reader = csv.DictReader(input_file)
        required_fields = {"timestamp_utc", "raw_temperature_celsius", "raw_humidity_percent"}
        if not reader.fieldnames or not required_fields.issubset(reader.fieldnames):
            raise ValueError(
                "Datalog CSV must contain timestamp_utc, raw_temperature_celsius, "
                "and raw_humidity_percent columns"
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                timestamp = parse_timestamp(row["timestamp_utc"])
                temperature = float(row["raw_temperature_celsius"])
                humidity = float(row["raw_humidity_percent"])
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid datalog row {line_number}: {error}") from error
            if not math.isfinite(temperature) or not math.isfinite(humidity):
                raise ValueError(f"Datalog row {line_number} contains a non-finite value")
            rows.append({
                "timestamp": timestamp,
                "temperature": temperature,
                "humidity": humidity,
            })
    if not rows:
        raise ValueError("Datalog CSV contains no samples")
    return rows


def read_calibration_file(path):
    rows = []
    with path.open(encoding="utf-8-sig") as calibration_file:
        for line_number, line in enumerate(calibration_file, start=1):
            fields = line.split()
            # sample.txt contains headings, alarm limits, summaries, and numbered
            # samples. Only numbered sample rows have reference T/RH and a time.
            if len(fields) < 5 or not fields[0].isdigit():
                continue
            try:
                timestamp = datetime.strptime(
                    " ".join(fields[-2:]), "%d/%m/%Y %H:%M:%S"
                ).astimezone()
                temperature = float(fields[1])
                humidity = float(fields[2])
            except (OverflowError, ValueError):
                continue
            if not math.isfinite(temperature) or not math.isfinite(humidity):
                raise ValueError(f"Calibration row {line_number} contains a non-finite value")
            rows.append({
                "timestamp": timestamp,
                "temperature": temperature,
                "humidity": humidity,
            })
    if not rows:
        raise ValueError(
            "Calibration file contains no numbered rows with TEMP(C), RH(%RH), "
            "and a trailing DD/MM/YYYY HH:MM:SS timestamp"
        )
    rows.sort(key=lambda row: row["timestamp"])
    return rows


def read_sample_files(paths, reader):
    rows = []
    for path in paths:
        rows.extend(reader(path))
    return sorted(rows, key=lambda row: row["timestamp"])


def pair_samples(datalog_rows, calibration_rows, max_time_difference):
    # Calibration times are sorted, so bisection narrows each datalog row to
    # the reference sample immediately before/after it. Matching is nearest in
    # time; a reference row may be reused, but pairs beyond the tolerance drop.
    calibration_times = [row["timestamp"].timestamp() for row in calibration_rows]
    pairs = []
    unmatched = 0
    for datalog_row in datalog_rows:
        input_time = datalog_row["timestamp"]
        input_seconds = input_time.timestamp()
        position = bisect.bisect_left(calibration_times, input_seconds)
        candidates = calibration_rows[max(0, position - 1):min(len(calibration_rows), position + 1)]
        closest = min(candidates, key=lambda row: abs((row["timestamp"] - input_time).total_seconds()))
        difference = abs((closest["timestamp"] - input_time).total_seconds())
        if difference > max_time_difference:
            unmatched += 1
            continue
        pairs.append({
            "input_timestamp": input_time.isoformat(timespec="seconds"),
            "reference_timestamp": closest["timestamp"].isoformat(timespec="seconds"),
            "time_difference_seconds": difference,
            "temperature_raw": datalog_row["temperature"],
            "temperature_reference": closest["temperature"],
            "humidity_raw": datalog_row["humidity"],
            "humidity_reference": closest["humidity"],
        })
    return pairs, unmatched


def solve_least_squares(points, degree):
    # A degree-n polynomial has n+1 unknown coefficients, so at least n+1
    # paired samples and distinct raw inputs are needed for a determined fit.
    if len(points) < degree + 1:
        raise ValueError(f"degree {degree} needs at least {degree + 1} paired samples")

    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    if len(set(x_values)) < degree + 1:
        raise ValueError(f"degree {degree} needs at least {degree + 1} distinct raw values")

    # Scale raw inputs to roughly [-1, 1] before fitting. Powers of values such
    # as 20 or 50 become poorly conditioned at higher degrees if fitted directly.
    x_min = min(x_values)
    x_max = max(x_values)
    scale = (x_max - x_min) / 2
    if scale == 0:
        raise ValueError("raw values have no range")
    center = (x_max + x_min) / 2
    normalized_x = [(value - center) / scale for value in x_values]

    # The Vandermonde design matrix has A[i,k] = z_i**k. Solve
    # min ||A*a - y||_2, where a contains coefficients in normalized x (z).
    column_count = degree + 1
    matrix = [
        [value ** power for power in range(column_count)]
        for value in normalized_x
    ]
    transformed_y = list(y_values)

    # Householder QR triangularizes A while preserving least-squares residuals.
    # It avoids forming A.T*A, which would square the matrix condition number.
    for pivot in range(column_count):
        vector = [matrix[row][pivot] for row in range(pivot, len(points))]
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if norm < 1e-14:
            raise ValueError(f"degree {degree} fit is numerically singular")
        # Choosing the sign to reinforce vector[0] reduces cancellation when
        # constructing the Householder reflection v.
        vector[0] += norm if vector[0] >= 0 else -norm
        vector_norm_squared = math.fsum(value * value for value in vector)
        if vector_norm_squared < 1e-28:
            raise ValueError(f"degree {degree} fit is numerically singular")
        factor = 2 / vector_norm_squared

        # Apply H = I - 2*v*v.T/(v.T*v) to the remaining matrix columns.
        for column in range(pivot, column_count):
            projection = factor * math.fsum(
                vector[offset] * matrix[pivot + offset][column]
                for offset in range(len(vector))
            )
            for offset, value in enumerate(vector):
                matrix[pivot + offset][column] -= projection * value

        # Apply the same reflection to y, producing Q.T*y alongside R.
        projection = factor * math.fsum(
            vector[offset] * transformed_y[pivot + offset]
            for offset in range(len(vector))
        )
        for offset, value in enumerate(vector):
            transformed_y[pivot + offset] -= projection * value

    # Back-substitute through the upper-triangular R to recover a in R*a=Q.T*y.
    normalized_coefficients = [0.0] * column_count
    for row in range(column_count - 1, -1, -1):
        remainder = math.fsum(
            matrix[row][column] * normalized_coefficients[column]
            for column in range(row + 1, column_count)
        )
        diagonal = matrix[row][row]
        if abs(diagonal) < 1e-14:
            raise ValueError(f"degree {degree} fit is numerically singular")
        normalized_coefficients[row] = (transformed_y[row] - remainder) / diagonal

    # Convert from z=(x-center)/scale to raw x. For each normalized term a_k*z^k,
    # expand (x-center)^k with the binomial theorem; its contribution to raw
    # coefficient b_j is a_k*C(k,j)*(-center)^(k-j)/scale^k. Runtime settings
    # store the resulting ordinary monomial coefficients as coef_0, coef_1, ... .
    coefficients = [0.0] * 7
    for normalized_power, coefficient in enumerate(normalized_coefficients):
        for raw_power in range(normalized_power + 1):
            coefficients[raw_power] += (
                coefficient
                * comb(normalized_power, raw_power)
                * (-center) ** (normalized_power - raw_power)
                / scale ** normalized_power
            )
    if not all(math.isfinite(value) for value in coefficients):
        raise ValueError(f"degree {degree} produced non-finite coefficients")
    return coefficients


def evaluate_polynomial(coefficients, degree, x_value):
    # Horner's form evaluates c0 + c1*x + ... + cn*x**n with fewer
    # multiplications and less rounding error than separately computing powers.
    result = coefficients[degree]
    for power in range(degree - 1, -1, -1):
        result = result * x_value + coefficients[power]
    return result


def fit_metrics(points, degree, coefficients):
    # Residual e_i is reference minus fitted HAT value. RMSE=sqrt(sum(e_i^2)/n)
    # penalizes large errors more than MAE=sum(|e_i|)/n. R^2=1-SSE/SST compares
    # residual error with variance around the reference mean; Pearson r measures
    # linear association, not agreement, so it is reported but not used to rank.
    actual = [target for _, target in points]
    predicted = [evaluate_polynomial(coefficients, degree, raw) for raw, _ in points]
    residuals = [target - estimate for target, estimate in zip(actual, predicted)]
    rmse = math.sqrt(math.fsum(value * value for value in residuals) / len(points))
    mae = math.fsum(abs(value) for value in residuals) / len(points)
    actual_mean = math.fsum(actual) / len(actual)
    predicted_mean = math.fsum(predicted) / len(predicted)
    total_sum_squares = math.fsum((value - actual_mean) ** 2 for value in actual)
    residual_sum_squares = math.fsum(value * value for value in residuals)
    r_squared = (
        1 - residual_sum_squares / total_sum_squares
        if total_sum_squares > 0 else None
    )
    covariance = math.fsum(
        (observed - actual_mean) * (estimate - predicted_mean)
        for observed, estimate in zip(actual, predicted)
    )
    actual_variance = math.fsum((value - actual_mean) ** 2 for value in actual)
    predicted_variance = math.fsum((value - predicted_mean) ** 2 for value in predicted)
    correlation = (
        covariance / math.sqrt(actual_variance * predicted_variance)
        if actual_variance > 0 and predicted_variance > 0 else None
    )
    return {
        "pearson_r": correlation,
        "r_squared": r_squared,
        "rmse": rmse,
        "mae": mae,
    }


def cross_validated_rmse(points, degree):
    # Hold out contiguous blocks instead of random rows because adjacent sensor
    # samples are time-correlated. The score estimates how well the polynomial
    # generalizes to unseen time segments; lower RMSE is better.
    sample_count = len(points)
    if sample_count < degree + 2:
        return None

    fold_count = sample_count if sample_count <= 20 else 5
    squared_errors = []
    for fold in range(fold_count):
        start = fold * sample_count // fold_count
        end = (fold + 1) * sample_count // fold_count
        training = points[:start] + points[end:]
        testing = points[start:end]
        try:
            coefficients = solve_least_squares(training, degree)
        except ValueError:
            return None
        squared_errors.extend(
            (target - evaluate_polynomial(coefficients, degree, raw)) ** 2
            for raw, target in testing
        )
    return math.sqrt(math.fsum(squared_errors) / len(squared_errors))


def detect_local_magnitude_outliers(values, sigma_threshold, window_radius, minimum_deviation):
    # Compare each point with nearby values, excluding the point itself. MAD is
    # the median absolute deviation from the local median; multiplying by 1.4826
    # makes it comparable to standard deviation for normally distributed noise.
    # The absolute floor avoids rejecting tiny quantization steps when MAD=0.
    outliers = {}
    for index, value in enumerate(values):
        start = max(0, index - window_radius)
        end = min(len(values), index + window_radius + 1)
        neighbors = values[start:index] + values[index + 1:end]
        if len(neighbors) < 3:
            continue

        local_median = statistics.median(neighbors)
        mad = statistics.median(abs(neighbor - local_median) for neighbor in neighbors)
        robust_sigma = 1.4826 * mad
        deviation = abs(value - local_median)
        cutoff = max(sigma_threshold * robust_sigma, minimum_deviation)
        if deviation > cutoff:
            outliers[index] = {
                "local_median": local_median,
                "robust_sigma_score": deviation / robust_sigma if robust_sigma else None,
                "deviation": deviation,
            }
    return outliers


def analyze_channel(
    name,
    pairs,
    config,
    degree_override,
    all_degrees,
    reject_outliers=False,
    outlier_sigma=3.5,
    outlier_window=3,
):
    channel = CHANNELS[name]
    original_points = [
        (pair[f"{name}_raw"], pair[f"{name}_reference"])
        for pair in pairs
    ]
    excluded_outliers = []
    if reject_outliers:
        # Test raw and reference streams independently. If either side is a
        # local spike, discard the entire matched calibration pair for this
        # channel so it cannot pull the fitted curve toward a bad measurement.
        raw_outliers = detect_local_magnitude_outliers(
            [raw for raw, _ in original_points],
            outlier_sigma,
            outlier_window,
            channel["outlier_min_deviation"],
        )
        reference_outliers = detect_local_magnitude_outliers(
            [reference for _, reference in original_points],
            outlier_sigma,
            outlier_window,
            channel["outlier_min_deviation"],
        )
        excluded_indices = set(raw_outliers) | set(reference_outliers)
        for index in sorted(excluded_indices):
            reasons = []
            if index in raw_outliers:
                reasons.append("raw")
            if index in reference_outliers:
                reasons.append("reference")
            excluded_outliers.append({
                "input_timestamp": pairs[index]["input_timestamp"],
                "reference_timestamp": pairs[index]["reference_timestamp"],
                "raw_value": original_points[index][0],
                "reference_value": original_points[index][1],
                "reasons": reasons,
                "raw_local_median": raw_outliers.get(index, {}).get("local_median"),
                "raw_robust_sigma_score": raw_outliers.get(index, {}).get("robust_sigma_score"),
                "raw_deviation": raw_outliers.get(index, {}).get("deviation"),
                "reference_local_median": reference_outliers.get(index, {}).get("local_median"),
                "reference_robust_sigma_score": reference_outliers.get(index, {}).get("robust_sigma_score"),
                "reference_deviation": reference_outliers.get(index, {}).get("deviation"),
            })
        keep_indices = [index for index in range(len(pairs)) if index not in excluded_indices]
        if len(keep_indices) < 4:
            raise ValueError(
                f"outlier rejection leaves too few {channel['label'].lower()} samples; "
                "disable --reject-outliers or increase --outlier-window"
            )
        pairs = [pairs[index] for index in keep_indices]
        points = [original_points[index] for index in keep_indices]
    else:
        points = original_points

    polynomial_config = config[channel["config_key"]]["polynomial"]
    degrees = (
        polynomial_config.get("available_degrees", range(0, 5))
        if all_degrees else [
            degree_override if degree_override is not None else polynomial_config["degree"]
        ]
    )

    models = []
    rejected_degrees = {}
    # Evaluate only the configured degree(s); --all-degrees uses the published
    # available_degrees list, while --degree overrides it for this run.
    for degree in degrees:
        if type(degree) is not int or not 0 <= degree <= 4:
            rejected_degrees[str(degree)] = "degree must be an integer from 0 to 4"
            continue
        try:
            coefficients = solve_least_squares(points, degree)
        except ValueError as error:
            rejected_degrees[str(degree)] = str(error)
            continue
        model = {
            "degree": degree,
            "coefficients": coefficients,
            "metrics": fit_metrics(points, degree, coefficients),
            "cross_validated_rmse": cross_validated_rmse(points, degree),
        }
        models.append(model)

    if not models:
        reasons = "; ".join(f"degree {degree}: {reason}" for degree, reason in rejected_degrees.items())
        raise ValueError(f"no usable {channel['label'].lower()} fit ({reasons})")

    if all_degrees:
        # Prefer models with a cross-validation score and choose its minimum.
        # If the sample count is too small for CV, fall back to training RMSE.
        cv_models = [model for model in models if model["cross_validated_rmse"] is not None]
        candidates = cv_models or models
        selected = min(
            candidates,
            key=lambda model: (
                model["cross_validated_rmse"]
                if model["cross_validated_rmse"] is not None
                else model["metrics"]["rmse"],
                model["degree"],
            ),
        )
        selection_metric = "cross_validated_rmse" if cv_models else "training_rmse"
    else:
        selected = models[0]
        selection_metric = "configured_degree"

    return {
        "matched_samples": len(original_points),
        "used_samples": len(points),
        "excluded_outlier_count": len(excluded_outliers),
        "excluded_outliers": excluded_outliers,
        "selected_degree": selected["degree"],
        "selection_metric": selection_metric,
        "suggested_degree": selected["degree"] if all_degrees else None,
        "suggestion_metric": selection_metric if all_degrees else None,
        "selected_coefficients": {
            f"coef_{power}": selected["coefficients"][power]
            for power in range(5)
        },
        "selected_metrics": selected["metrics"],
        "degree_results": [
            {
                "degree": model["degree"],
                **model["metrics"],
                "cross_validated_rmse": model["cross_validated_rmse"],
                "coefficients": {
                    f"coef_{power}": model["coefficients"][power]
                    for power in range(5)
                },
            }
            for model in models
        ],
        "rejected_degrees": rejected_degrees,
        "_fit": {
            "degree": selected["degree"],
            "coefficients": selected["coefficients"],
            "points": points,
            "pairs": pairs,
        },
    }


def print_channel_result(name, result, show_fit, previous_polynomial):
    print(
        f"{CHANNELS[name]['label']}: {result['used_samples']} of "
        f"{result['matched_samples']} paired samples used"
    )
    if result["excluded_outlier_count"]:
        print(f"  excluded local magnitude outliers: {result['excluded_outlier_count']}")
    for model in result["degree_results"]:
        correlation = model["pearson_r"]
        correlation_text = "n/a" if correlation is None else f"{correlation:.6f}"
        print(
            f"  degree {model['degree']}: r={correlation_text}, "
            f"R²={model['r_squared'] if model['r_squared'] is not None else 'n/a'}, "
            f"RMSE={model['rmse']:.6g}, CV RMSE="
            f"{model['cross_validated_rmse'] if model['cross_validated_rmse'] is not None else 'n/a'}"
        )
    selected_degree = result["selected_degree"]
    print(f"  selected degree: {selected_degree} ({result['selection_metric']})")
    suggested_degree = result["suggested_degree"]
    if suggested_degree is None:
        print("  suggested degree: unavailable (run with --all-degrees)")
    else:
        print(f"  suggested degree: {suggested_degree} ({result['suggestion_metric']})")
    print("  coefficients:")
    for name, value in result["selected_coefficients"].items():
        print(f"    {name} = {value:.12g}")

    print("  previous configuration vs fitted result:")
    print(f"    {'setting':<10} {'current':>18} {'fitted':>18}")
    print(
        f"    {'degree':<10} {previous_polynomial['degree']:>18} "
        f"{result['selected_degree']:>18}"
    )
    for setting, fitted_value in result["selected_coefficients"].items():
        previous_value = previous_polynomial.get(setting, 0.0)
        print(f"    {setting:<10} {previous_value:>18.12g} {fitted_value:>18.12g}")

    if show_fit:
        fit = result["_fit"]
        print("  input time (local) | raw | reference | fitted | residual")
        for pair, (raw, reference) in zip(fit["pairs"], fit["points"]):
            fitted = evaluate_polynomial(fit["coefficients"], fit["degree"], raw)
            print(
                f"    {pair['input_timestamp']} | {raw:.6g} | {reference:.6g} | "
                f"{fitted:.6g} | {reference - fitted:+.6g}"
            )


def update_config(path, config, channel_results):
    for name, result in channel_results.items():
        channel_config = config[name]
        polynomial = channel_config["polynomial"]
        polynomial["degree"] = result["selected_degree"]
        polynomial.update(result["selected_coefficients"])
        channel_config["calibration_enabled"] = True

    original_mode = stat.S_IMODE(path.stat().st_mode)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as config_file:
            json.dump(config, config_file, indent=2)
            config_file.write("\n")
            temporary_path = Path(config_file.name)
        os.chmod(temporary_path, original_mode)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2, allow_nan=False)
        report_file.write("\n")


def timestamped_report_path(path, generated_at):
    timestamp = generated_at.strftime("%Y%m%d_%H%M%SZ")
    if path.suffix:
        return path.with_name(f"{path.stem}_{timestamp}{path.suffix}")
    return path / f"calibration-report_{timestamp}.json"


def timestamped_plot_path(input_path, report_path, channel, generated_at):
    if report_path is not None:
        return report_path.with_name(f"{report_path.stem}_{channel}.svg")
    timestamp = generated_at.strftime("%Y%m%d_%H%M%SZ")
    return input_path.with_name(f"{input_path.stem}_calibration_{timestamp}_{channel}.svg")


def write_svg_plot(path, channel_name, result):
    points = result["_fit"]["points"]
    models = sorted(
        result["degree_results"],
        key=lambda model: (
            model["cross_validated_rmse"] is None,
            model["cross_validated_rmse"]
            if model["cross_validated_rmse"] is not None
            else model["rmse"],
            model["degree"],
        ),
    )
    recommended_degree = result["suggested_degree"]
    highlighted_degree = recommended_degree if recommended_degree is not None else result["selected_degree"]
    x_values = [raw for raw, _ in points]
    observed_values = [reference for _, reference in points]
    x_min = min(x_values)
    x_max = max(x_values)
    if x_min == x_max:
        x_min -= 0.5
        x_max += 0.5

    curve_x = [x_min + (x_max - x_min) * step / 200 for step in range(201)]
    curve_y_by_degree = {
        model["degree"]: [
            evaluate_polynomial(
                [model["coefficients"][f"coef_{power}"] for power in range(5)],
                model["degree"],
                value,
            )
            for value in curve_x
        ]
        for model in models
    }
    all_curve_values = [value for curve in curve_y_by_degree.values() for value in curve]
    y_min = min(observed_values + all_curve_values)
    y_max = max(observed_values + all_curve_values)
    y_span = y_max - y_min
    margin = y_span * 0.08 if y_span else 1.0
    y_min -= margin
    y_max += margin

    width = 960
    height = 640
    left = 100
    right = 36
    top = 205
    bottom = 82
    plot_width = width - left - right
    plot_height = height - top - bottom

    def x_position(value):
        return left + (value - x_min) / (x_max - x_min) * plot_width

    def y_position(value):
        return top + (y_max - value) / (y_max - y_min) * plot_height

    channel = CHANNELS[channel_name]
    x_label = "Raw temperature (C)" if channel_name == "temperature" else "Raw humidity (%RH)"
    y_label = "Reference temperature (C)" if channel_name == "temperature" else "Reference humidity (%RH)"
    metrics = result["selected_metrics"]
    correlation = metrics["pearson_r"]
    correlation_text = "n/a" if correlation is None else f"{correlation:.5f}"
    degree_label = "recommended" if recommended_degree is not None else "selected"

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<rect width='100%' height='100%' fill='white'/>",
        f'<text x="{left}" y="34" font-family="sans-serif" font-size="22" font-weight="bold">{escape(channel["label"])} calibration fit</text>',
        f'<text x="{left}" y="60" font-family="sans-serif" font-size="14">{degree_label} degree {highlighted_degree} | Pearson r {correlation_text} | RMSE {metrics["rmse"]:.5g} | n={len(points)}</text>',
    ]

    for tick in range(6):
        fraction = tick / 5
        y_value = y_min + (y_max - y_min) * fraction
        y = y_position(y_value)
        svg.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" stroke="#d8dee8"/>')
        svg.append(f'<text x="{left - 12}" y="{y + 5:.2f}" text-anchor="end" font-family="sans-serif" font-size="12">{y_value:.4g}</text>')

    for tick in range(6):
        fraction = tick / 5
        x_value = x_min + (x_max - x_min) * fraction
        x = x_position(x_value)
        svg.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{height - bottom}" stroke="#edf0f4"/>')
        svg.append(f'<text x="{x:.2f}" y="{height - bottom + 24}" text-anchor="middle" font-family="sans-serif" font-size="12">{x_value:.4g}</text>')

    svg.append(f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#252a34" stroke-width="1.5"/>')
    svg.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#252a34" stroke-width="1.5"/>')
    alternate_colors = ("#64748b", "#2f83a8", "#8b6bb1", "#9b7b39", "#507b58")
    model_colors = {}
    for model_index, model in enumerate(models):
        model_degree = model["degree"]
        is_highlighted = model_degree == highlighted_degree
        color = "#d04a35" if is_highlighted else alternate_colors[model_index % len(alternate_colors)]
        model_colors[model_degree] = color

    # Draw worse curves first so the recommended curve remains visible on top.
    for model in reversed(models):
        model_degree = model["degree"]
        is_highlighted = model_degree == highlighted_degree
        color = model_colors[model_degree]
        polyline_points = " ".join(
            f"{x_position(x_value):.2f},{y_position(y_value):.2f}"
            for x_value, y_value in zip(curve_x, curve_y_by_degree[model_degree])
        )
        svg.append(
            f'<polyline points="{polyline_points}" fill="none" stroke="{color}" '
            f'stroke-width="{4 if is_highlighted else 2}"/>'
        )
    for x_value, reference in points:
        svg.append(f'<circle cx="{x_position(x_value):.2f}" cy="{y_position(reference):.2f}" r="4" fill="#176b87" fill-opacity="0.78"/>')
    svg.append(f'<text x="{left + plot_width / 2:.2f}" y="{height - 18}" text-anchor="middle" font-family="sans-serif" font-size="15">{x_label}</text>')
    svg.append(f'<text x="24" y="{top + plot_height / 2:.2f}" transform="rotate(-90 24 {top + plot_height / 2:.2f})" text-anchor="middle" font-family="sans-serif" font-size="15">{y_label}</text>')
    for rank, model in enumerate(models, start=1):
        model_degree = model["degree"]
        is_highlighted = model_degree == highlighted_degree
        color = model_colors[model_degree]
        y = 88 + (rank - 1) * 18
        score = model["cross_validated_rmse"]
        score_label = "CV RMSE" if score is not None else "RMSE"
        score_value = score if score is not None else model["rmse"]
        recommendation = " (suggested)" if is_highlighted and recommended_degree is not None else ""
        svg.append(f'<line x1="{left}" y1="{y - 4}" x2="{left + 20}" y2="{y - 4}" stroke="{color}" stroke-width="{4 if is_highlighted else 2}"/>')
        svg.append(f'<text x="{left + 27}" y="{y}" font-family="sans-serif" font-size="11">{rank}. degree {model_degree}{recommendation} | {score_label} {score_value:.5g}</text>')
    reference_y = 88 + len(models) * 18
    svg.append(f'<circle cx="{left + 4}" cy="{reference_y - 4}" r="4" fill="#176b87"/><text x="{left + 27}" y="{reference_y}" font-family="sans-serif" font-size="11">reference samples</text>')
    svg.append("</svg>")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def timestamped_data_plot_path(input_path, report_path, channel, data_selection, generated_at):
    if report_path is not None:
        return report_path.with_name(f"{report_path.stem}_{channel}_data_{data_selection}.svg")
    timestamp = generated_at.strftime("%Y%m%d_%H%M%SZ")
    return input_path.with_name(
        f"{input_path.stem}_calibration_{timestamp}_{channel}_data_{data_selection}.svg"
    )


def write_data_svg_plot(path, channel_name, data_selection, datalog_rows, calibration_rows, fit_result=None):
    channel = CHANNELS[channel_name]
    input_start = min(row["timestamp"] for row in datalog_rows)
    input_end = max(row["timestamp"] for row in datalog_rows)
    calibration_rows = [
        row for row in calibration_rows
        if input_start <= row["timestamp"] <= input_end
    ]
    fitted_pairs = fit_result["_fit"]["pairs"] if fit_result is not None else None

    series = []
    if data_selection in {"raw", "both"}:
        raw_values = (
            [
                (parse_timestamp(pair["input_timestamp"]), pair[f"{channel_name}_raw"])
                for pair in fitted_pairs
            ]
            if fitted_pairs is not None
            else [(row["timestamp"], row[channel_name]) for row in datalog_rows]
        )
        series.append((
            "Raw datalog",
            raw_values,
            "#176b87",
        ))
    if data_selection in {"calibration", "both"}:
        reference_values = (
            [
                (parse_timestamp(pair["reference_timestamp"]), pair[f"{channel_name}_reference"])
                for pair in fitted_pairs
            ]
            if fitted_pairs is not None
            else [(row["timestamp"], row[channel_name]) for row in calibration_rows]
        )
        if not reference_values:
            raise ValueError(
                f"no calibration samples for {channel_name} fall within the datalog time range"
            )
        series.append((
            "Calibration reference",
            reference_values,
            "#9b7b39",
        ))
    if fit_result is not None:
        fit = fit_result["_fit"]
        series.append((
            f"Adjusted HAT (degree {fit['degree']})",
            [
                (
                    parse_timestamp(pair["input_timestamp"]),
                    evaluate_polynomial(fit["coefficients"], fit["degree"], raw),
                )
                for pair, (raw, _) in zip(fit["pairs"], fit["points"])
            ],
            "#d04a35",
        ))

    all_times = [timestamp.timestamp() for _, values, _ in series for timestamp, _ in values]
    all_values = [value for _, values, _ in series for _, value in values]
    x_min = min(all_times)
    x_max = max(all_times)
    if x_min == x_max:
        x_min -= 30
        x_max += 30
    y_min = min(all_values)
    y_max = max(all_values)
    y_span = y_max - y_min
    y_margin = y_span * 0.08 if y_span else 1.0
    y_min -= y_margin
    y_max += y_margin

    width = 1080
    height = 640
    left = 100
    right = 42
    top = 82
    bottom = 88
    plot_width = width - left - right
    plot_height = height - top - bottom

    def x_position(value):
        return left + (value - x_min) / (x_max - x_min) * plot_width

    def y_position(value):
        return top + (y_max - value) / (y_max - y_min) * plot_height

    if channel_name == "temperature":
        title = "Temperature raw and calibration data"
        y_label = "Temperature (C)"
    else:
        title = "Humidity raw and calibration data"
        y_label = "Humidity (%RH)"

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<rect width='100%' height='100%' fill='white'/>",
        f'<text x="{left}" y="34" font-family="sans-serif" font-size="22" font-weight="bold">{escape(title)}</text>',
        f'<text x="{left}" y="58" font-family="sans-serif" font-size="13">Shown: {escape(data_selection)} | local time | calibration samples restricted to datalog time range</text>',
    ]

    for tick in range(6):
        fraction = tick / 5
        value = y_min + (y_max - y_min) * fraction
        y = y_position(value)
        svg.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" stroke="#d8dee8"/>')
        svg.append(f'<text x="{left - 12}" y="{y + 5:.2f}" text-anchor="end" font-family="sans-serif" font-size="12">{value:.5g}</text>')

    for tick in range(6):
        fraction = tick / 5
        value = x_min + (x_max - x_min) * fraction
        x = x_position(value)
        label = datetime.fromtimestamp(value).astimezone().strftime("%H:%M:%S")
        svg.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{height - bottom}" stroke="#edf0f4"/>')
        svg.append(f'<text x="{x:.2f}" y="{height - bottom + 23}" text-anchor="middle" font-family="sans-serif" font-size="12">{label}</text>')

    svg.append(f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#252a34" stroke-width="1.5"/>')
    svg.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#252a34" stroke-width="1.5"/>')
    for series_index, (series_name, values, color) in enumerate(series):
        polyline_points = " ".join(
            f"{x_position(timestamp.timestamp()):.2f},{y_position(value):.2f}"
            for timestamp, value in values
        )
        svg.append(f'<polyline points="{polyline_points}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        for timestamp, value in values:
            svg.append(f'<circle cx="{x_position(timestamp.timestamp()):.2f}" cy="{y_position(value):.2f}" r="3.5" fill="{color}"/>')
        legend_y = 55 + series_index * 23
        svg.append(f'<line x1="{width - 290}" y1="{legend_y}" x2="{width - 260}" y2="{legend_y}" stroke="{color}" stroke-width="3"/>')
        svg.append(f'<text x="{width - 252}" y="{legend_y + 5}" font-family="sans-serif" font-size="12">{escape(series_name)} (n={len(values)})</text>')

    svg.append(f'<text x="{left + plot_width / 2:.2f}" y="{height - 18}" text-anchor="middle" font-family="sans-serif" font-size="15">Local time</text>')
    svg.append(f'<text x="24" y="{top + plot_height / 2:.2f}" transform="rotate(-90 24 {top + plot_height / 2:.2f})" text-anchor="middle" font-family="sans-serif" font-size="15">{escape(y_label)}</text>')
    svg.append("</svg>")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def write_combined_svg_plot(path, channel_name, result, datalog_rows, calibration_rows):
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    with tempfile.TemporaryDirectory() as temporary_directory:
        fit_path = Path(temporary_directory) / "fit.svg"
        data_path = Path(temporary_directory) / "data.svg"
        write_svg_plot(fit_path, channel_name, result)
        write_data_svg_plot(
            data_path,
            channel_name,
            "both",
            datalog_rows,
            calibration_rows,
            fit_result=result,
        )
        fit_root = ET.parse(fit_path).getroot()
        data_root = ET.parse(data_path).getroot()

    namespace = "http://www.w3.org/2000/svg"
    combined = ET.Element(
        f"{{{namespace}}}svg",
        {"width": "960", "height": "1220", "viewBox": "0 0 960 1220"},
    )
    fit_panel = ET.SubElement(
        combined,
        f"{{{namespace}}}svg",
        {"x": "0", "y": "0", "width": "960", "height": "640", "viewBox": "0 0 960 640"},
    )
    fit_panel.extend(list(fit_root))
    data_panel = ET.SubElement(
        combined,
        f"{{{namespace}}}svg",
        {"x": "0", "y": "650", "width": "960", "height": "569", "viewBox": "0 0 1080 640"},
    )
    data_panel.extend(list(data_root))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ET.tostring(combined, encoding="utf-8", xml_declaration=True))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Fit temperature and humidity corrections from datalog and reference samples."
    )
    parser.add_argument("--version", action="version", version=f"calibrate {CALIBRATE_VERSION} (CGweather 0.4.1)")
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        nargs="+",
        action="extend",
        help="One or more datalog CSV paths",
    )
    parser.add_argument(
        "--calibration",
        required=True,
        type=Path,
        nargs="+",
        action="extend",
        help="One or more reference sample paths",
    )
    parser.add_argument(
        "--channel",
        choices=("temperature", "humidity", "both"),
        default="both",
        help="Correction channel to fit (default: both; pressure is not supported)",
    )
    degree_group = parser.add_mutually_exclusive_group()
    degree_group.add_argument("--degree", type=int, choices=range(0, 5), help="Override the degree in settings")
    degree_group.add_argument("--all-degrees", action="store_true", help="Compare all configured degrees")
    parser.add_argument(
        "--max-time-difference-seconds",
        type=float,
        default=30.0,
        help="Maximum nearest-reference timestamp difference (default: 30 seconds)",
    )
    parser.add_argument(
        "--reject-outliers",
        action="store_true",
        help="Exclude local magnitude spikes using a rolling median/MAD filter",
    )
    parser.add_argument(
        "--outlier-sigma",
        type=float,
        default=3.5,
        help="Robust-sigma cutoff for magnitude outliers (default: 3.5)",
    )
    parser.add_argument(
        "--outlier-window",
        type=int,
        default=3,
        help="Neighbor count on each side for outlier detection (default: 3)",
    )
    parser.add_argument("--show-fit", action="store_true", help="Print each selected model's fitted samples")
    parser.add_argument("--plot", action="store_true", help="Write SVG fit plots for viewing on this PC")
    parser.add_argument(
        "--plot-data",
        nargs="?",
        choices=("raw", "calibration", "both"),
        const="both",
        help="Write time-series SVG plots for raw, calibration, or both (default: both)",
    )
    parser.add_argument("--update-config", action="store_true", help="Save selected coefficients and polynomial methods to settings")
    parser.add_argument("--report-json", type=Path, help="Write a JSON report to this path")
    parser.add_argument("--config", type=Path, default=DEFAULT_SETTINGS_PATH, help="Settings JSON path")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.max_time_difference_seconds) or args.max_time_difference_seconds < 0:
            raise ValueError("maximum time difference must be a finite non-negative number")
        if not math.isfinite(args.outlier_sigma) or args.outlier_sigma <= 0:
            raise ValueError("outlier sigma must be a finite positive number")
        if args.outlier_window < 2:
            raise ValueError("outlier window must be at least 2 samples per side")
        with args.config.open(encoding="utf-8") as config_file:
            config = json.load(config_file)

        datalog_rows = read_sample_files(args.input, read_datalog)
        calibration_rows = read_sample_files(args.calibration, read_calibration_file)
        pairs, unmatched_rows = pair_samples(
            datalog_rows, calibration_rows, args.max_time_difference_seconds
        )
        if not pairs:
            raise ValueError(
                "no paired samples found; check that both files cover the same local-time period "
                "or increase --max-time-difference-seconds"
            )

        channel_names = tuple(CHANNELS) if args.channel == "both" else (args.channel,)
        channel_results = {}
        for name in channel_names:
            channel_results[name] = analyze_channel(
                name,
                pairs,
                config,
                args.degree,
                args.all_degrees,
                args.reject_outliers,
                args.outlier_sigma,
                args.outlier_window,
            )

        generated_at = datetime.now(timezone.utc)
        report_path = timestamped_report_path(args.report_json, generated_at) if args.report_json else None
        report = {
            "generated_at_utc": generated_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "input_file": (
                str(args.input[0].resolve()) if len(args.input) == 1
                else [str(path.resolve()) for path in args.input]
            ),
            "calibration_file": (
                str(args.calibration[0].resolve()) if len(args.calibration) == 1
                else [str(path.resolve()) for path in args.calibration]
            ),
            "settings_file": str(args.config.resolve()),
            "local_timezone": str(datetime.now().astimezone().tzinfo),
            "channel_selection": args.channel,
            "max_time_difference_seconds": args.max_time_difference_seconds,
            "time_rejections": {
                "outside_max_time_difference": unmatched_rows,
                "max_time_difference_seconds": args.max_time_difference_seconds,
            },
            "magnitude_outlier_filter": {
                "enabled": args.reject_outliers,
                "method": "local Hampel/MAD" if args.reject_outliers else None,
                "sigma_threshold": args.outlier_sigma if args.reject_outliers else None,
                "window_radius": args.outlier_window if args.reject_outliers else None,
                "minimum_deviation": {
                    "temperature_celsius": CHANNELS["temperature"]["outlier_min_deviation"],
                    "humidity_percent": CHANNELS["humidity"]["outlier_min_deviation"],
                } if args.reject_outliers else None,
            },
            "datalog_rows": len(datalog_rows),
            "calibration_rows": len(calibration_rows),
            "matched_rows": len(pairs),
            "unmatched_datalog_rows": unmatched_rows,
            "channels": {
                name: {
                    key: value
                    for key, value in result.items()
                    if key != "_fit"
                }
                for name, result in channel_results.items()
            },
        }

        plot_files = {}
        if args.plot:
            for name, result in channel_results.items():
                plot_path = timestamped_plot_path(args.input[0], report_path, name, generated_at)
                write_combined_svg_plot(
                    plot_path, name, result, datalog_rows, calibration_rows
                )
                plot_files[name] = str(plot_path.resolve())
                print(f"{CHANNELS[name]['label']} combined plot written to {plot_path}")

        if args.plot_data:
            for name in channel_names:
                plot_path = timestamped_data_plot_path(
                    args.input[0], report_path, name, args.plot_data, generated_at
                )
                write_data_svg_plot(
                    plot_path,
                    name,
                    args.plot_data,
                    datalog_rows,
                    calibration_rows,
                    fit_result=channel_results[name] if args.reject_outliers else None,
                )
                plot_files[f"{name}_{args.plot_data}_data"] = str(plot_path.resolve())
                print(f"{CHANNELS[name]['label']} {args.plot_data} data plot written to {plot_path}")

        if plot_files:
            report["plot_files"] = plot_files

        if report_path is not None:
            write_report(report_path, report)
            print(f"JSON report written to {report_path}")

        print(
            f"Paired {len(pairs)} of {len(datalog_rows)} datalog rows "
            f"({unmatched_rows} outside the time tolerance)."
        )
        for name, result in channel_results.items():
            previous_polynomial = config[name]["polynomial"]
            print_channel_result(name, result, args.show_fit, previous_polynomial)

        if args.update_config:
            update_config(args.config, config, channel_results)
            print(f"Updated polynomial settings in {args.config}")
    except (OSError, csv.Error, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
import os
import json
import torch
import shutil
from pathlib import Path
from diffusers import OnnxRuntimeModel, OnnxStableDiffusionPipeline, StableDiffusionPipeline
from olive.model import ONNXModel
from olive.workflows import run as olive_run

from modules import shared
from modules.paths_internal import sd_configs_path, models_path

available_sampling_methods = ["pndm", "lms", "heun", "euler", "euler-ancestral", "dpm", "ddim"]

def optimize_from_ckpt(checkpoint: str, vae_id: str, vae_subfolder: str, unoptimized_dir: str, optimized_dir: str, safety_checker: bool, text_encoder: bool, unet: bool, vae_decoder: bool, vae_encoder: bool, scheduler_type: str, use_fp16: bool, sample_height_dim: int, sample_width_dim: int, sample_height: int, sample_width: int, olive_merge_lora: bool, *olive_merge_lora_inputs):
    unoptimized_dir = Path(models_path) / "ONNX" / unoptimized_dir
    optimized_dir = Path(models_path) / "ONNX-Olive" / optimized_dir
    
    shutil.rmtree("footprints", ignore_errors=True)
    shutil.rmtree(unoptimized_dir, ignore_errors=True)
    shutil.rmtree(optimized_dir, ignore_errors=True)

    pipeline = StableDiffusionPipeline.from_ckpt(os.path.join(models_path, "Stable-diffusion", checkpoint), torch_dtype=torch.float32, requires_safety_checker=False, scheduler_type=scheduler_type)
    pipeline.save_pretrained(unoptimized_dir)

    optimize(unoptimized_dir, optimized_dir, pipeline, vae_id, vae_subfolder, safety_checker, text_encoder, unet, vae_decoder, vae_encoder, use_fp16, sample_height_dim, sample_width_dim, sample_height, sample_width, olive_merge_lora, *olive_merge_lora_inputs)

def optimize_from_onnx(model_id: str, vae_id: str, vae_subfolder: str, unoptimized_dir: str, optimized_dir: str, safety_checker: bool, text_encoder: bool, unet: bool, vae_decoder: bool, vae_encoder: bool, use_fp16: bool, sample_height_dim: int, sample_width_dim: int, sample_height: int, sample_width: int, olive_merge_lora: bool, *olive_merge_lora_inputs):
    unoptimized_dir = Path(models_path) / "ONNX" / unoptimized_dir
    optimized_dir = Path(models_path) / "ONNX-Olive" / optimized_dir
    
    shutil.rmtree("footprints", ignore_errors=True)
    shutil.rmtree(optimized_dir, ignore_errors=True)

    if os.path.isdir(unoptimized_dir):
        pipeline = StableDiffusionPipeline.from_pretrained(unoptimized_dir, torch_dtype=torch.float32, requires_safety_checker=False)
    else:
        pipeline = StableDiffusionPipeline.from_pretrained(model_id, torch_dtype=torch.float32, requires_safety_checker=False)
        pipeline.save_pretrained(unoptimized_dir)

    optimize(unoptimized_dir, optimized_dir, pipeline, vae_id, vae_subfolder, safety_checker, text_encoder, unet, vae_decoder, vae_encoder, use_fp16, sample_height_dim, sample_width_dim, sample_height, sample_width, olive_merge_lora, *olive_merge_lora_inputs)

def optimize(unoptimized_dir: Path, optimized_dir: Path, pipeline, vae_id: str, vae_subfolder: str, safety_checker: bool, text_encoder: bool, unet: bool, vae_decoder: bool, vae_encoder: bool, use_fp16: bool, sample_height_dim: int, sample_width_dim: int, sample_height: int, sample_width: int, olive_merge_lora: bool, *olive_merge_lora_inputs):
    model_info = {}
    submodels = []

    if safety_checker:
        submodels += ["safety_checker"]
    if text_encoder:
        submodels += ["text_encoder"]
    if unet:
        submodels += ["unet"]
    if vae_decoder:
        submodels += ["vae_decoder"]
    if vae_encoder:
        submodels += ["vae_encoder"]

    os.environ["OLIVE_CKPT_PATH"] = str(unoptimized_dir)
    os.environ["OLIVE_VAE"] = vae_id or str(unoptimized_dir)
    os.environ["OLIVE_VAE_SUBFOLDER"] = vae_subfolder
    os.environ["OLIVE_SAMPLE_HEIGHT_DIM"] = str(sample_height_dim)
    os.environ["OLIVE_SAMPLE_WIDTH_DIM"] = str(sample_width_dim)
    os.environ["OLIVE_SAMPLE_HEIGHT"] = str(sample_height)
    os.environ["OLIVE_SAMPLE_WIDTH"] = str(sample_width)
    os.environ["OLIVE_LORA_BASE_PATH"] = str(Path(models_path) / "Lora")
    if olive_merge_lora:
        os.environ["OLIVE_LORAS"] = '$'.join(olive_merge_lora_inputs)

    for submodel_name in submodels:
        print(f"\nOptimizing {submodel_name}")

        with open(Path(sd_configs_path) / "olive_optimize" / f"config_{submodel_name}.json", "r") as olive_config_raw:
            olive_config = json.load(olive_config_raw)
        olive_config["passes"]["optimize"]["config"]["float16"] = use_fp16

        olive_run(olive_config)

        footprints_file_path = (
            Path("footprints") / f"{submodel_name}_gpu-dml_footprints.json"
        )
        with footprints_file_path.open("r") as footprint_file:
            footprints = json.load(footprint_file)

            conversion_footprint = None
            optimizer_footprint = None
            for _, footprint in footprints.items():
                if footprint["from_pass"] == "OnnxConversion":
                    conversion_footprint = footprint
                elif footprint["from_pass"] == "OrtTransformersOptimization":
                    optimizer_footprint = footprint

            assert conversion_footprint and optimizer_footprint

            unoptimized_olive_model = ONNXModel(**conversion_footprint["model_config"]["config"])
            optimized_olive_model = ONNXModel(**optimizer_footprint["model_config"]["config"])

            model_info[submodel_name] = {
                "unoptimized": {
                    "path": Path(unoptimized_olive_model.model_path),
                },
                "optimized": {
                    "path": Path(optimized_olive_model.model_path),
                },
            }

            print(f"Optimized {submodel_name}")

    print("\nCreating ONNX pipeline...")
    onnx_pipeline = OnnxStableDiffusionPipeline(
        vae_encoder=OnnxRuntimeModel.from_pretrained(model_info["vae_encoder"]["unoptimized"]["path"].parent),
        vae_decoder=OnnxRuntimeModel.from_pretrained(model_info["vae_decoder"]["unoptimized"]["path"].parent),
        text_encoder=OnnxRuntimeModel.from_pretrained(model_info["text_encoder"]["unoptimized"]["path"].parent),
        tokenizer=pipeline.tokenizer,
        unet=OnnxRuntimeModel.from_pretrained(model_info["unet"]["unoptimized"]["path"].parent),
        scheduler=pipeline.scheduler,
        safety_checker=OnnxRuntimeModel.from_pretrained(model_info["safety_checker"]["unoptimized"]["path"].parent) if safety_checker else None,
        feature_extractor=pipeline.feature_extractor,
        requires_safety_checker=False,
    )

    print("Saving unoptimized models...")
    onnx_pipeline.save_pretrained(unoptimized_dir)

    print("Copying optimized models...")
    shutil.copytree(unoptimized_dir, optimized_dir, ignore=shutil.ignore_patterns("weights.pb"))
    for submodel_name in submodels:
        try:
            src_path = model_info[submodel_name]["optimized"]["path"]
            dst_path = optimized_dir / submodel_name / "model.onnx"
            shutil.copyfile(src_path, dst_path)
        except:
            pass

    with open(optimized_dir / "opt_config.json", "w") as opt_config:
        json.dump({
            "sample_height_dim": sample_height_dim,
            "sample_width_dim": sample_width_dim,
            "sample_height": sample_height,
            "sample_width": sample_width,
        }, opt_config)

    shared.refresh_checkpoints()
    print(f"Optimization complete.")
#!/usr/bin/env python3

import sys
import argparse
from datetime import datetime

import grpc
from containerd.services.containers.v1 import containers_pb2_grpc, containers_pb2
from containerd.services.images.v1 import images_pb2_grpc, images_pb2
from containerd.services.content.v1 import content_pb2_grpc, content_pb2

def compute_size(contentv1, imgDigest, doneLayer=None):
    content = contentv1.Info( content_pb2.InfoRequest(digest=imgDigest),
                              metadata=(('containerd-namespace', 'k8s.io'),)).info
    layers = [l for l in content.labels if "containerd.io/gc.ref.content." in l]
    size = content.size
    for l in layers:
        try:
            if doneLayer is not None:
                if content.labels[l] in doneLayer:
                    continue
                else:
                    doneLayer.append(content.labels[l])
                    size += compute_size(contentv1, content.labels[l], doneLayer)
            else:
                size += compute_size(contentv1, content.labels[l])
        except:
            pass # Layer not found in content ?
    return size

# From Fred Cirera on StackOverflow : thanks !
# https://stackoverflow.com/questions/1094841/get-human-readable-version-of-file-size
def sizeof_fmt(num, suffix='B'):
    for unit in [' B',' K',' M',' G',' T',' P',' E',' Z']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)

parser = argparse.ArgumentParser(prog="Prune image for microk8s")
parser.add_argument("-c", "--listcontainers", action="store_true",
    help="print containers and associated images")
parser.add_argument("-i", "--listimages", action="store_true",
    help="print images name")
parser.add_argument("-u", "--listunused", action="store_true",
    help="print unused images name")
parser.add_argument("-s", "--info", action="store_true",
    help="print sumary info")
parser.add_argument("-p", "--prune", action="store_true",
    help="DELETE unused images (if run interactively, will request confirmation)")
parser.add_argument("-f", "--force", action="store_true",
    help="force delete without confirmation")
args = parser.parse_args()
if not any(vars(args).values()):
    parser.print_help()
    sys.exit()

if args.prune and not args.force and sys.stdout.isatty():
    resp = input("Unused images will be deleted, please confirm [y/N]: ")
    if resp.upper() != "Y":
        print("Operation cancelled")
        sys.exit(1)

grpc_options = [('grpc.max_receive_message_length', 32 * 1024 * 1024)]  # 32MB

with grpc.insecure_channel('unix:///var/snap/microk8s/common/run/containerd.sock', options=grpc_options) as channel:

    containersv1 = containers_pb2_grpc.ContainersStub(channel)
    imagesv1 = images_pb2_grpc.ImagesStub(channel)
    contentv1 = content_pb2_grpc.ContentStub(channel)

    containers = containersv1.List( containers_pb2.ListContainersRequest(),
                                    metadata=(('containerd-namespace', 'k8s.io'),)).containers

    usedImages = {}
    for c in containers:
        usedImages[c.image] = c.id
        if args.listcontainers: print("C:", c.id, c.image)

    images = imagesv1.List( images_pb2.ListImagesRequest(),
                            metadata=(('containerd-namespace', 'k8s.io'),)).images

    unused = []
    totalImageSize = 0
    netTotalSize = 0
    doneLayer = []
    for i in images:
        if i.name not in usedImages: unused.append(i.name)
        imageSize = compute_size(contentv1, i.target.digest)
        totalImageSize += imageSize
        netTotalSize += compute_size(contentv1, i.target.digest, doneLayer)
        if args.listimages:
            print("I:", i.name,
                        imageSize,
                        datetime.fromtimestamp(i.updated_at.seconds).isoformat())

    if args.listunused:
        for i in unused: print("U:", i)

    if args.prune:
        for i in unused: imagesv1.Delete( images_pb2.DeleteImageRequest( name=i, sync=True),
                                          metadata=(('containerd-namespace', 'k8s.io'),) )

    if args.info:

        print("S:", len(containers), "containers")
        print("S:", len(images), "total images")
        print("S: %s (%s bytes) total images size, %s shared" % (sizeof_fmt(totalImageSize), totalImageSize, sizeof_fmt(totalImageSize-netTotalSize)))
        print("S:", len(usedImages), "used images")
        if unused: print("S:", len(unused), "unused images")

        if args.prune:
            images = imagesv1.List( images_pb2.ListImagesRequest(),
                                    metadata=(('containerd-namespace', 'k8s.io'),)).images
            newNetImageSize = 0
            doneLayer = []
            for i in images:
                newNetImageSize += compute_size(contentv1, i.target.digest, doneLayer)
            recovered = netTotalSize - newNetImageSize
            print("S: %s (%s bytes) recovered space" % (sizeof_fmt(recovered), recovered))

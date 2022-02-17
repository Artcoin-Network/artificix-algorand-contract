""" PyTeal to escrow asset and get stable coin aUSD. """
# TODO: make sure that ASSET and STABLE have the same decimals, otherwise this can happen: 1e-8 ART <-> 1e-4 aUSD

from pyteal import (
    Add,
    And,
    App,
    Bytes,
    Cond,
    Div,
    Ed25519Verify,
    Global,
    If,
    InnerTxnBuilder,
    Int,
    Minus,
    Mode,
    Mul,
    OnComplete,
    Return,
    ScratchVar,
    Seq,
    ShiftRight,
    TealType,
    Txn,
    TxnField,
    compileTeal,
)

from ..classes.algorand import TealCmdList, TealPackage, TealParam
from ..resources import (
    ASSET_NAME,
    STABLE_NAME,
    SUM_ASSET,
    SUM_STABLE,
    # ASSET_ID,
    # STABLE_ID,
)

ASSET_ID = 9
STABLE_ID = 10


CRDB = 32  # CRDB is the number of bytes in the CRD
CRD = Int(
    1 << CRDB
)  # collateralisation ratio denominator, reciprocal of precision of CR.

local_ints_scheme = [ASSET_NAME, "aUSD"]  # to check if user can burn / need escrow more
local_bytes_scheme = [
    "history"
]  # not needed at redeem: no more data for more data, maybe more "blocks"?
global_ints_scheme = {
    SUM_ASSET: f"sum of {ASSET_NAME} collateral, with unit of decimal.",
    SUM_STABLE: f"sum of {STABLE_NAME} issued, with unit of decimal.",
    "CRN": "collateralisation ratio = numerator / 2^32, \
        in range [0,2^32] with precision of 2^-32 (too fine precision).",
    # collateralisation ratio numerator
    # TODO:discuss: precision 2^-16 should be enough, we don't need that much.
    # TODO:+: Decimal is clearer. 2^-16 ~== 0.0015%. the floating range is much larger.
}
global_bytes_scheme = ["price_info"]  # origin of price, implementation of ZKP.

teal_param: TealParam = {
    "local_ints": len(local_ints_scheme),
    "local_bytes": len(local_bytes_scheme),
    "global_ints": len(global_ints_scheme),
    "global_bytes": len(global_bytes_scheme),
}

cmd_list: TealCmdList = [
    ["escrow"],  # send $ART$ to escrow
    ["redeem"],  # redeem $ART$ from escrow
]


def approval_program():
    handle_creation = Seq(
        [
            App.globalPut(Bytes(SUM_ASSET), Int(0)),
            App.globalPut(Bytes(SUM_STABLE), Int(0)),
            App.globalPut(
                Bytes("CRN"), Int(5 << 32)
            ),  # == 5* 2**32, assuming 1$ART$=1aUSD, minting 5$ART$ to get 1aUSD.
            Return(Int(1)),
        ]
    )  # global_ints_scheme

    handle_opt_in = Return(Int(1))  # always allow user to opt in

    handle_close_out = Return(
        Int(0)  # not allow anyone to close out escrow for now, for todo
    )  # TODO: check user's escrow. Only user with 0 escrow can close out (not losing $ART$).

    handle_update_app = Return(
        And(
            Global.creator_address() == Txn.sender(),  # TODO: manager
            Ed25519Verify(
                data=Txn.application_args[0],
                sig=Txn.application_args[1],
                key=Txn.application_args[2],
            ),  # security, should use a password, high cost is ok.
        )
    )

    handle_deleteapp = handle_update_app  # Same security level as update_app

    scratch_sum_asset = ScratchVar(TealType.uint64)  # used in both escrow and redeem
    scratch_sum_stable = ScratchVar(TealType.uint64)  # used in both escrow and redeem
    scratch_CRN = ScratchVar(TealType.uint64)  # used in both escrow and redeem
    scratch_issuing = ScratchVar(TealType.uint64)  # aUSD, only used in escrow
    scratch_returning = ScratchVar(TealType.uint64)  # $ART$, only used in redeem
    escrow = Seq(
        # User escrow $ART$ to get aUSD
        [
            scratch_sum_asset.store(
                App.globalGet(Bytes(SUM_ASSET))
            ),  # TODO:ask: why store?
            scratch_sum_stable.store(App.globalGet(Bytes(SUM_STABLE))),
            scratch_CRN.store(App.globalGet(Bytes("CRN"))),
            scratch_issuing.store(
                ShiftRight(Div(Txn.asset_amount(), scratch_CRN.load()), Int(CRDB))
            ),  # Remember that all needs Int()!
            # Issue aUSD to user
            InnerTxnBuilder.Begin(),  # TODO: not sure if this is correct
            InnerTxnBuilder.SetFields(
                {
                    # TODO:bug: :down: this line has some problem
                    # TxnField.note: Bytes(f"issuance of aUSD on TXN_ID: {Txn.tx_id()}"),
                    TxnField.xfer_asset: Int(STABLE_ID),
                    TxnField.asset_amount: scratch_issuing.load(),
                    TxnField.sender: Global.creator_address(),
                    TxnField.asset_receiver: Txn.sender(),
                    TxnField.asset_close_to: Global.creator_address(),
                }
            ),
            InnerTxnBuilder.Submit(),  # Issue aUSD to user
            App.localPut(
                Txn.sender(),
                Bytes(ASSET_NAME),
                Add(App.localGet(Txn.sender(), Bytes(ASSET_NAME)), Txn.asset_amount()),
            ),
            App.localPut(
                Txn.sender(),
                Bytes(STABLE_NAME),
                Add(
                    App.localGet(Txn.sender(), Bytes(STABLE_NAME)),
                    scratch_issuing.load(),
                ),
            ),
            App.globalPut(
                Bytes(SUM_ASSET),
                Add(scratch_sum_asset.load(), (Txn.asset_amount())),
            ),
            App.globalPut(
                Bytes(SUM_STABLE),
                Add(scratch_sum_stable.load(), scratch_issuing.load()),
            ),
            Return(Int(1)),
        ]
    )

    redeem = Seq(
        # user redeem $ART$ from aUSD, assuming user has enough escrowed $ART$
        [
            scratch_sum_asset.store(App.globalGet(Bytes(SUM_ASSET))),
            scratch_sum_stable.store(App.globalGet(Bytes(SUM_STABLE))),
            scratch_CRN.store(App.globalGet(Bytes("CRN"))),
            # TODO:fix: check asset == $ART$, not any random asset
            scratch_returning.store(
                ShiftRight(Mul((Txn.asset_amount()), scratch_CRN.load()), Int(CRDB))
            ),
            # Issue aUSD to user
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields(
                {
                    # TODO:bug: :down: this line has some problem
                    TxnField.note: Bytes(f"issuance of aUSD on TXN_ID: {Txn.tx_id()}"),
                    TxnField.xfer_asset: Int(ASSET_ID),
                    TxnField.asset_amount: scratch_returning.load(),
                    TxnField.asset_receiver: Txn.sender(),
                    TxnField.sender: Global.creator_address(),
                    TxnField.asset_close_to: Global.creator_address(),
                }
            ),
            InnerTxnBuilder.Submit(),  # Issue aUSD to user
            App.localPut(
                Txn.sender(),
                Bytes(ASSET_NAME),
                Minus(
                    App.localGet(Txn.sender(), Bytes(ASSET_NAME)),
                    Txn.asset_amount(),
                ),
            ),
            App.localPut(
                Txn.sender(),
                Bytes(STABLE_NAME),
                Minus(
                    App.localGet(Txn.sender(), Bytes(STABLE_NAME)),
                    scratch_returning.load(),
                ),
            ),
            App.globalPut(
                Bytes(SUM_ASSET),
                Minus(
                    scratch_sum_asset.load(),
                    (Txn.asset_amount()),
                ),
            ),
            App.globalPut(
                Bytes(SUM_STABLE),
                Minus(
                    scratch_sum_stable.load(),
                    scratch_returning.load(),
                ),
            ),
            Return(Int(1)),
        ]
    )

    handle_no_op = Cond(
        [
            And(
                Global.group_size() == Int(1),
                Txn.application_args[0] == Bytes("escrow"),
            ),
            escrow,
        ],
        [
            And(
                Global.group_size() == Int(1),
                Txn.application_args[0] == Bytes("redeem"),
                App.localGet(Txn.sender(), Bytes(STABLE_NAME)) >= Txn.asset_amount(),
                # correct logic depend on ACID (atomicity, consistency, isolation, durability).
                # cannot be used to cheat (will not parallel) for ACID.
            ),
            redeem,
        ],
        [
            Int(1),
            Return(Int(0)),  # Fail if no correct args.
        ],
    )

    program = Cond(
        [Txn.application_id() == Int(0), handle_creation],
        [Txn.on_completion() == OnComplete.OptIn, handle_opt_in],
        [Txn.on_completion() == OnComplete.CloseOut, handle_close_out],
        [Txn.on_completion() == OnComplete.UpdateApplication, handle_update_app],
        [Txn.on_completion() == OnComplete.DeleteApplication, handle_deleteapp],
        [Txn.on_completion() == OnComplete.NoOp, handle_no_op],
        # checked picture https://github.com/algorand/docs/blob/92d2bb3929d2301e1d3acfd164b0621593fcac5b/docs/imgs/sccalltypes.png
    )
    # Mode.Application specifies that this is a smart contract
    return compileTeal(program, Mode.Application, version=5)


def clear_program():
    program = Return(Int(1))
    # Mode.Application specifies that this is a smart contract
    return compileTeal(program, Mode.Application, version=5)


# print out the results
# print(approval_program())
# print(clear_program())

escrow_package = TealPackage(
    "escrow", approval_program(), clear_program(), teal_param, cmd_list
)
